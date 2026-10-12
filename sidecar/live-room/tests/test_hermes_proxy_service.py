from unittest.mock import Mock, patch
from app.hermes_proxy_service import HermesProxyService

def test_existing_proxy_is_never_owned_or_stopped():
    service=HermesProxyService()
    with patch('app.hermes_proxy_service.WINDOWS',True),patch.dict('os.environ',{'LOCALAPPDATA':'/qa','ZEN_HERMES_URL':'http://127.0.0.1:8645','ZEN_HERMES_PROXY_AUTOSTART':'1'}),patch('app.hermes_proxy_service.Path.is_file',return_value=True),patch('app.hermes_proxy_service.socket.create_connection') as connection,patch('app.hermes_proxy_service.subprocess.Popen') as launch,patch('app.hermes_proxy_service.subprocess.run') as stop:
        service.start();service.stop()
    connection.assert_called_once();launch.assert_not_called();stop.assert_not_called()

def test_disabled_or_custom_endpoint_never_launches():
    for setting in ({'ZEN_HERMES_PROXY_AUTOSTART':'0'},{'ZEN_HERMES_URL':'http://localhost:9000'}):
        with patch.dict('os.environ',setting),patch('app.hermes_proxy_service.WINDOWS',True),patch('app.hermes_proxy_service.subprocess.Popen') as launch:
            HermesProxyService().start()
        launch.assert_not_called()

def test_only_owned_command_tree_is_stopped():
    service=HermesProxyService();process=Mock(pid=12345);process.poll.return_value=None
    with patch('app.hermes_proxy_service.WINDOWS',True),patch.dict('os.environ',{'ZEN_HERMES_URL':'http://127.0.0.1:8645','ZEN_HERMES_PROXY_AUTOSTART':'1','LOCALAPPDATA':'C:/Users/Test/AppData/Local'}),patch('app.hermes_proxy_service.Path.is_file',return_value=True),patch('app.hermes_proxy_service.socket.create_connection',side_effect=OSError),patch('app.hermes_proxy_service.subprocess.Popen',return_value=process) as launch,patch('app.hermes_proxy_service.subprocess.run') as stop,patch('app.hermes_proxy_service.subprocess.CREATE_NO_WINDOW',0,create=True):
        service.start();service.stop();service.stop()
    assert launch.call_args.args[0][0].endswith('hermes.cmd')
    assert launch.call_args.args[0][-4:]==['--host','127.0.0.1','--port','8645']
    stop.assert_called_once();assert stop.call_args.args[0]==['taskkill','/PID','12345','/T','/F']
    process.wait.assert_called_once();assert service.process is None

def test_failed_optional_start_does_not_stop_caption_service():
    with patch('app.hermes_proxy_service.WINDOWS',True),patch.dict('os.environ',{'ZEN_HERMES_URL':'http://127.0.0.1:8645','ZEN_HERMES_PROXY_AUTOSTART':'1','LOCALAPPDATA':'C:/Users/Test/AppData/Local'}),patch('app.hermes_proxy_service.Path.is_file',return_value=True),patch('app.hermes_proxy_service.socket.create_connection',side_effect=OSError),patch('app.hermes_proxy_service.subprocess.Popen',side_effect=OSError),patch('app.hermes_proxy_service.subprocess.CREATE_NO_WINDOW',0,create=True):
        service=HermesProxyService();service.start();service.stop()
    assert service.process is None
