"""ASCII file arguments for native tools that use Windows' ANSI argv."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


class NativePaths:
    def __init__(self, model: Path):
        self.cwd = model.resolve().parent if os.name == "nt" else None
        self.temporary = None
        self.aliases = 0

    def workspace(self) -> Path:
        if self.temporary is None:
            self.temporary = tempfile.TemporaryDirectory(prefix="breeze-native-", dir=self.cwd)
        return Path(self.temporary.name)

    def argument(self, path: Path, *, allow_copy: bool = False) -> str:
        if self.cwd is None:
            return str(path)
        absolute = path.resolve()
        try:
            relative = os.path.relpath(absolute, self.cwd)
        except ValueError:  # A supplied recording can be on another drive.
            relative = str(absolute)
        if relative.isascii():
            return relative
        # Hard links don't duplicate a 1GB model. Short audio can be copied when
        # it lives on another drive; aliases disappear after the native process.
        self.aliases += 1
        suffix = path.suffix if path.suffix.isascii() else ".bin"
        alias = self.workspace() / ("asset-" + str(self.aliases) + suffix)
        try:
            os.link(absolute, alias)
        except OSError as exc:
            if not allow_copy:
                if shutil.disk_usage(self.workspace()).free < absolute.stat().st_size + 64 * 1024 * 1024:
                    raise OSError("模型檔名含中文且此目錄無法建立連結；請改用英數字模型檔名，或預留空間供暫存複本。") from exc
            shutil.copyfile(absolute, alias)
        return os.path.relpath(alias, self.cwd)

    def cli_command(self, binary: Path, arguments: list[str]) -> list[str]:
        if self.cwd is None:
            return [str(binary), *arguments]
        # whisper-cli v1.9.2 reads UTF-8 response-file lines. This preserves a
        # Chinese prompt independently of the Windows machine's ANSI code page.
        response = self.workspace() / "arguments.txt"
        response.write_text("\n".join(arg.replace("\r", " ").replace("\n", " ") for arg in arguments) + "\n", encoding="utf-8")
        return [str(binary.resolve()), "@" + os.path.relpath(response, self.cwd)]

    def close(self) -> None:
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None
