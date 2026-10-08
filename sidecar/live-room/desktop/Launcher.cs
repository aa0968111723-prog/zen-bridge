using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal sealed class BreezeWindow : Form {
    readonly string root = AppDomain.CurrentDomain.BaseDirectory;
    readonly string instance = Guid.NewGuid().ToString("N");
    readonly HttpClient http = new HttpClient(new HttpClientHandler { UseProxy = false });
    readonly Label status = new Label { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter, ForeColor = Color.White, BackColor = Color.FromArgb(16,24,39), Font = new Font("Microsoft JhengHei UI", 16), Text = "正在準備中文辨識\n第一次大約要半分鐘，完成後會自動進入。\n請先不要關閉這個視窗。" };
    readonly WebView2 browser = new WebView2 { Dock = DockStyle.Fill, Visible = false };
    readonly MenuStrip menu = new MenuStrip();
    Process service;
    IntPtr job;
    bool closing, stopped, ready, jobAssigned;
    string url, rememberedError;
    int port;
    readonly bool smoke = Array.IndexOf(Environment.GetCommandLineArgs(), "--smoke-test") >= 0;

    [STAThread] static void Main() {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        bool owned;
        using (var mutex = new Mutex(true, "Local\\BreezeLiveRoomApp", out owned)) {
            if (!owned) { MessageBox.Show("Breeze 已經開啟，請切換到現有視窗。", "Breeze"); return; }
            Application.Run(new BreezeWindow());
        }
    }

    BreezeWindow() {
        Text = "禪譯聽眾房";
        Width = 1260; Height = 840; MinimumSize = new Size(880, 600);
        StartPosition = FormStartPosition.CenterScreen;
        string icon = Path.Combine(root, "desktop", "breeze.ico");
        if (File.Exists(icon)) Icon = new Icon(icon);
        menu.Items.Add("檢查更新", null, async delegate { await CheckUpdates(); });
        menu.Items.Add("字幕資料", null, delegate { Process.Start("explorer.exe", "\"" + Path.Combine(root, "data") + "\""); });
        menu.Items.Add("怎麼用", null, delegate { MessageBox.Show("1. 按「開始聽」，並允許麥克風。\n2. 對著電腦說話，下面會出現中文字幕。\n3. 手機連同一個 Wi-Fi，掃描畫面上的 QR。\n\n關閉此視窗會停止字幕。", "怎麼用"); });
        menu.Items.Add("關於", null, delegate { MessageBox.Show("禪譯聽眾房 " + File.ReadAllText(Path.Combine(root,"VERSION")).Trim() + "\n本機中文字幕 · 同一個 Wi-Fi 的手機可看\n關閉此視窗會停止字幕服務。", "關於"); });
        Controls.Add(browser); Controls.Add(status); Controls.Add(menu);
        MainMenuStrip = menu;
        Shown += async delegate { await StartApp(); };
        FormClosing += async (sender, e) => {
            if (stopped) return;
            e.Cancel = true;
            if (closing) return;
            closing = true; ready = false; menu.Enabled = false;
            browser.Visible = false; status.Visible = true; status.Text = "正在關閉字幕服務…";
            await StopService();
            browser.Dispose(); http.Dispose(); stopped = true; Close();
        };
    }

    async Task StartApp() {
        bool failed = false;
        try {
            Directory.CreateDirectory(Path.Combine(root,"data"));
            Directory.CreateDirectory(Path.Combine(root,"logs"));
            port = 8780;
            var envFile = Path.Combine(root,".env");
            if (File.Exists(envFile)) foreach (var raw in File.ReadAllLines(envFile, Encoding.UTF8)) {
                var line = raw.Trim();
                if (line.StartsWith("BREEZE_PORT=")) port = int.Parse(line.Substring(12).Trim().Trim('"','\''));
            }
            var portEnvironment = Environment.GetEnvironmentVariable("BREEZE_PORT");
            if (!String.IsNullOrWhiteSpace(portEnvironment)) port = int.Parse(portEnvironment);
            var probe = new TcpListener(IPAddress.Any, port);
            try { probe.Start(); }
            catch (SocketException) { throw new Exception("連接埠 " + port + " 已被占用。請關閉另一個 Breeze 後再開。"); }
            finally { try { probe.Stop(); } catch { } }
            if (!File.Exists(Path.Combine(Environment.SystemDirectory, "msvcp140.dll"))) throw new Exception("缺少 Microsoft Visual C++ x64 執行階段。請先安裝後再開。");
            try { CoreWebView2Environment.GetAvailableBrowserVersionString(); }
            catch (WebView2RuntimeNotFoundException) { throw new Exception("這台電腦沒有 Microsoft WebView2。請重新執行安裝程式，或安裝 Evergreen Runtime 後再開。"); }
            url = "http://127.0.0.1:" + port;
            string python = PythonPath();
            var info = new ProcessStartInfo(python, "-m app.desktop_service") { WorkingDirectory=root, UseShellExecute=false, CreateNoWindow=true, RedirectStandardInput=true, RedirectStandardOutput=true, RedirectStandardError=true };
            info.EnvironmentVariables["PYTHONUTF8"] = "1";
            info.EnvironmentVariables["BREEZE_OPEN_BROWSER"] = "0";
            info.EnvironmentVariables["BREEZE_ASR"] = "native";
            info.EnvironmentVariables["BREEZE_DESKTOP_INSTANCE"] = instance;
            info.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
            info.EnvironmentVariables.Remove("SSLKEYLOGFILE");
            info.EnvironmentVariables.Remove("PYTHONPATH");
            info.EnvironmentVariables.Remove("PYTHONHOME");
            info.EnvironmentVariables.Remove("VIRTUAL_ENV");
            job = CreateJobObject(IntPtr.Zero, null);
            var limits = new ExtendedLimits(); limits.Basic.LimitFlags = 0x2000;
            int size = Marshal.SizeOf(limits); IntPtr ptr = Marshal.AllocHGlobal(size);
            try { Marshal.StructureToPtr(limits,ptr,false); if (!SetInformationJobObject(job,9,ptr,(uint)size)) throw new Exception("無法建立程序管理。 "); }
            finally { Marshal.FreeHGlobal(ptr); }
            service = Process.Start(info);
            if (service == null) throw new Exception("無法啟動字幕服務。");
            jobAssigned = AssignProcessToJobObject(job, service.Handle);
            if (!jobAssigned) Log("無法納入程序管理工作，關閉時改以程序樹停止。");
            service.OutputDataReceived += (s,e) => { }; service.ErrorDataReceived += (s,e) => Log(e.Data);
            service.BeginOutputReadLine(); service.BeginErrorReadLine();
            service.StandardInput.WriteLine("start"); service.StandardInput.Flush();
            http.Timeout = TimeSpan.FromSeconds(2);
            var deadline = DateTime.UtcNow.AddSeconds(210);
            while (!closing && DateTime.UtcNow < deadline) {
                if (service.HasExited) throw new Exception("字幕服務提早結束。請查看 logs\\app.log。退出碼：" + service.ExitCode);
                try {
                    var response = await http.GetAsync(url + "/api/health");
                    string body = await response.Content.ReadAsStringAsync();
                    var parsed = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(body);
                    if (parsed != null && parsed.ContainsKey("instance_id") && Convert.ToString(parsed["instance_id"]) == instance) {
                        if (parsed.ContainsKey("error") && parsed["error"] != null) rememberedError = Convert.ToString(parsed["error"]);
                        if (parsed.ContainsKey("ready") && parsed["ready"] is bool && (bool)parsed["ready"]) { ready = true; break; }
                        if (!String.IsNullOrEmpty(rememberedError)) throw new Exception(rememberedError);
                    }
                } catch (HttpRequestException) { } catch (TaskCanceledException) { } catch (InvalidOperationException) { } catch (ArgumentException) { }
                await Task.Delay(350);
            }
            if (closing) return;
            if (!ready) throw new Exception(String.IsNullOrEmpty(rememberedError) ? "辨識還沒準備好。請關閉後再開一次；若仍失敗，重新執行安裝程式。" : rememberedError);
            var environment = await CoreWebView2Environment.CreateAsync(null, Path.Combine(root,"data","WebView2"));
            await browser.EnsureCoreWebView2Async(environment);
            browser.CoreWebView2.Settings.AreDevToolsEnabled = false;
            browser.CoreWebView2.Settings.IsStatusBarEnabled = false;
            browser.CoreWebView2.NavigationStarting += (s,e) => {
                Uri target; if (Uri.TryCreate(e.Uri,UriKind.Absolute,out target) && target.GetLeftPart(UriPartial.Authority) != url) e.Cancel = true;
            };
            browser.CoreWebView2.NewWindowRequested += (s,e) => { e.Handled = true; Uri target; if (Uri.TryCreate(e.Uri,UriKind.Absolute,out target) && (target.Scheme=="http" || target.Scheme=="https")) OpenWeb(e.Uri); };
            browser.CoreWebView2.PermissionRequested += (s,e) => { if (!e.Uri.StartsWith(url+"/") || e.PermissionKind != CoreWebView2PermissionKind.Microphone) e.State = CoreWebView2PermissionState.Deny; };
            browser.CoreWebView2.NavigationCompleted += async (s,e) => {
                if (!e.IsSuccess) { Log("WebView navigation failed: " + e.WebErrorStatus); return; }
                if (smoke) {
                    string result = await browser.CoreWebView2.ExecuteScriptAsync("JSON.stringify({title:document.title,body:document.body.innerText.length,microphone:!!navigator.mediaDevices})");
                    File.WriteAllText(Path.Combine(root,"data","desktop-smoke.json"),result,Encoding.UTF8);
                    await Task.Delay(1000); Close();
                }
            };
            status.Visible = false; browser.Visible = true; browser.CoreWebView2.Navigate(url + "/");
        } catch (Exception ex) {
            Log(ex.ToString());
            status.Text = "啟動沒有完成\n\n" + ex.Message + "\n\n可以關閉視窗後再開一次，或重新執行安裝程式。";
            failed = true;
        }
        if (failed) { await StopService(); if (smoke) { Environment.ExitCode = 1; Close(); } }
    }

    string PythonPath() {
        string venv = Path.Combine(root,".venv","Scripts","python.exe");
        if (File.Exists(venv)) return venv;
        var manifest = new JavaScriptSerializer().Deserialize<dynamic>(File.ReadAllText(Path.Combine(root,"bootstrap-manifest.json")));
        return Path.Combine(root,".python",(string)manifest["python"]["version"],"tools","python.exe");
    }
    void Log(string line) {
        if (String.IsNullOrEmpty(line)) return;
        try { lock (this) { string path=Path.Combine(root,"logs","app.log"); if (File.Exists(path) && new FileInfo(path).Length>2*1024*1024) File.WriteAllText(path,""); File.AppendAllText(path,line+Environment.NewLine,Encoding.UTF8); } } catch { }
    }
    async Task StopService() {
        if (service != null) {
            try { if (!service.HasExited) { service.StandardInput.WriteLine("stop"); service.StandardInput.Flush(); await Task.Run(()=>service.WaitForExit(6000)); } } catch { }
            if (!service.HasExited) KillTree();
        }
        if (job != IntPtr.Zero) { CloseHandle(job); job = IntPtr.Zero; }
    }
    void KillTree() {
        try {
            var killer = Process.Start(new ProcessStartInfo("taskkill.exe", "/PID " + service.Id + " /T /F") { CreateNoWindow = true, UseShellExecute = false });
            if (killer != null) killer.WaitForExit(4000);
        } catch { try { service.Kill(); } catch { } }
    }
    async Task<Dictionary<string, object>> UpdateCommand(string action, int timeoutMs) {
        string python = PythonPath();
        if (!File.Exists(python)) throw new Exception("找不到 App 內的 Python，無法檢查更新。");
        var info = new ProcessStartInfo(python, "-m app.desktop_update " + action) { WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
        info.EnvironmentVariables["PYTHONUTF8"] = "1";
        info.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
        info.EnvironmentVariables.Remove("SSLKEYLOGFILE");
        info.EnvironmentVariables.Remove("PYTHONPATH");
        info.EnvironmentVariables.Remove("PYTHONHOME");
        info.EnvironmentVariables.Remove("VIRTUAL_ENV");
        var proc = Process.Start(info);
        if (proc == null) throw new Exception("無法啟動更新程式。");
        var stdoutTask = proc.StandardOutput.ReadToEndAsync();
        var stderrTask = proc.StandardError.ReadToEndAsync();
        bool finished = await Task.Run(() => proc.WaitForExit(timeoutMs));
        if (!finished) { try { proc.Kill(); } catch { } throw new Exception("檢查更新逾時。"); }
        string stdout = await stdoutTask;
        await stderrTask;
        string line = "";
        foreach (var item in stdout.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries)) line = item;
        if (line == "") throw new Exception("更新程式沒有回應。");
        return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(line);
    }
    async Task CheckUpdates() {
        menu.Enabled = false;
        bool restore = browser.Visible;
        status.Visible = true;
        status.Text = "正在檢查更新…";
        if (restore) browser.Visible = false;
        try {
            var parsed = await UpdateCommand("--check", 60000);
            string state = Convert.ToString(parsed["status"]);
            if (state == "current") { MessageBox.Show("目前已是最新正式版本。", "Breeze 更新"); return; }
            if (state == "unavailable") { MessageBox.Show(Convert.ToString(parsed["message"]), "Breeze 更新"); return; }
            if (state == "error") throw new Exception(Convert.ToString(parsed["message"]));
            if (state != "update") throw new Exception("無法判斷更新狀態。");
            string version = Convert.ToString(parsed["version"]);
            if (MessageBox.Show("可更新至 " + version + "。安裝程式會先核對 SHA256，並保留設定與字幕。現在下載並安裝？", "Breeze 更新", MessageBoxButtons.YesNo) != DialogResult.Yes) return;
            status.Text = "正在下載並核對安裝檔…";
            parsed = await UpdateCommand("--download", 1800000);
            state = Convert.ToString(parsed["status"]);
            if (state != "ready") throw new Exception(parsed.ContainsKey("message") ? Convert.ToString(parsed["message"]) : "下載未完成。");
            string path = Convert.ToString(parsed["path"]);
            if (!File.Exists(path) || Path.GetFileName(path) != "Breeze-Live-Room-Setup.exe") throw new Exception("更新檔無效。");
            await StopService();
            stopped = true;
            Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
            Environment.Exit(0);
        } catch (Exception ex) { MessageBox.Show("檢查更新失敗：" + ex.Message, "Breeze 更新"); }
        finally { if (!closing && !stopped) { menu.Enabled = true; status.Visible = !restore; browser.Visible = restore; if (!restore) status.Text = "正在啟動 Breeze…\n首次載入模型需要一些時間。"; } }
    }
    static void OpenWeb(string value) { Process.Start(new ProcessStartInfo(value) { UseShellExecute=true }); }
    [StructLayout(LayoutKind.Sequential)] struct BasicLimits { public long ProcessTime,JobTime; public uint LimitFlags; public UIntPtr MinWorkingSet,MaxWorkingSet; public uint ActiveProcesses; public UIntPtr Affinity; public uint Priority,Scheduling; }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong ReadOps,WriteOps,OtherOps,ReadBytes,WriteBytes,OtherBytes; }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits { public BasicLimits Basic; public IoCounters Io; public UIntPtr ProcessMemory,JobMemory,PeakProcess,PeakJob; }
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attrs,string name);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job,int kind,IntPtr info,uint length);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
}
