"""Own an optional loopback subscription proxy without changing the Hermes gateway."""
import logging
import os
from pathlib import Path
import socket
import subprocess

log=logging.getLogger('zen.hermes.proxy')
WINDOWS=os.name=='nt'

class HermesProxyService:
    def __init__(self,port=8645):
        if not isinstance(port,int) or not 1024<=port<=65535:raise ValueError('Invalid proxy port')
        self.port=port
        self.process=None

    def start(self):
        if not WINDOWS or self.process is not None or os.getenv('ZEN_HERMES_PROXY_AUTOSTART','1')=='0':return
        if os.getenv('ZEN_HERMES_URL','http://127.0.0.1:8645').rstrip('/')!=f'http://127.0.0.1:{self.port}':return
        local=os.getenv('LOCALAPPDATA')
        if not local:return
        launcher=Path(local)/'hermes/bin/hermes.cmd'
        if not launcher.is_file():return
        try:
            with socket.create_connection(('127.0.0.1',self.port),timeout=.5):return
        except OSError:pass
        try:
            # Use the official synchronous .cmd entrypoint. The .exe launcher
            # detaches its child, preventing reliable ownership on shutdown.
            self.process=subprocess.Popen([str(launcher),'proxy','start','--provider','xai',
                '--host','127.0.0.1','--port',str(self.port)],shell=True,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            log.info('App-owned Hermes subscription proxy starting')
        except OSError:
            log.warning('Optional Hermes subscription proxy could not start')

    def stop(self):
        process,self.process=self.process,None
        if process is None or process.poll() is not None:return
        try:
            # Only the process tree created by this App is stopped. An already
            # listening proxy and the default Hermes/Telegram gateway are unowned.
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,timeout=10,check=False)
            process.wait(timeout=5)
        except (OSError,subprocess.TimeoutExpired):
            log.warning('App-owned Hermes proxy shutdown did not complete')
