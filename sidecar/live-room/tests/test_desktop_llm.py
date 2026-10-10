import io
import unittest
from unittest.mock import patch
from app.desktop_llm import DesktopTranslator, loopback_url


class DesktopTranslatorTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {
            'ZEN_LOCAL_LLM_URL': 'http://127.0.0.1:11434',
            'ZEN_HERMES_URL': 'http://127.0.0.1:8645',
            'ZEN_HERMES_API_KEY': '',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = DesktopTranslator()

    def test_endpoints_are_loopback_only(self):
        for url in ['https://127.0.0.1', 'http://example.com',
                    'http://name:secret@localhost', 'http://localhost?key=x',
                    'http://localhost#fragment']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                loopback_url(url)
        self.assertEqual(loopback_url('http://127.0.0.1:11434/'), 'http://127.0.0.1:11434')

    def test_local_mode_never_calls_cloud_on_failure(self):
        with patch.object(self.client, '_request', side_effect=OSError) as request:
            with self.assertRaises(RuntimeError):
                self.client.translate('Translate', 'hello', 'local')
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0], self.client.local)

    def test_cloud_mode_never_calls_local(self):
        result = {'choices': [{'message': {'content': 'translated'}, 'finish_reason': 'stop'}]}
        with patch.object(self.client, '_request', return_value=result) as request:
            self.assertEqual(self.client.translate('Translate', 'hello', 'cloud')['route'], 'cloud')
            self.assertEqual(request.call_args.args[0], self.client.hermes)

    def test_only_hybrid_permits_fallback(self):
        result = {'choices': [{'message': {'content': 'translated'}, 'finish_reason': 'stop'}]}
        with patch.object(self.client, '_request', side_effect=[OSError, result]):
            response = self.client.translate('Translate', 'hello', 'hybrid')
        self.assertTrue(response['fallback'])
        self.assertEqual(response['route'], 'cloud')

    def test_invalid_input_never_calls_provider(self):
        with patch.object(self.client, '_request') as request:
            for instructions, text, mode in [('x', '', 'local'), ('x', 'x', 'unknown'), ('x'*6001, 'x', 'local'), ('x', 'x'*24001, 'local')]:
                with self.assertRaises(ValueError):
                    self.client.translate(instructions, text, mode)
            request.assert_not_called()

    def test_truncated_cloud_response_is_rejected(self):
        result = {'choices': [{'message': {'content': 'partial'}, 'finish_reason': 'length'}]}
        with patch.object(self.client, '_request', return_value=result), self.assertRaises(RuntimeError):
            self.client.translate('Translate', 'hello', 'cloud')

    def test_truncated_local_response_is_rejected(self):
        result = {'message': {'content': 'partial'}, 'done_reason': 'length'}
        with patch.object(self.client, '_request', return_value=result), self.assertRaises(RuntimeError):
            self.client.translate('Translate', 'hello', 'local')

    def test_instruction_echo_is_not_a_translation(self):
        with patch.object(self.client, '_request', return_value={'message': {'content': 'Translate'}}), self.assertRaises(RuntimeError):
            self.client.translate('Translate', 'hello', 'local')

    def test_status_requires_actual_upstream_authentication(self):
        with patch.object(self.client.opener, 'open', return_value=io.BytesIO(b'{"authenticated":false}')) as request:
            status = self.client.status()
            self.assertTrue(status['hermes_reachable'])
            self.assertFalse(status['hermes_auth_configured'])
            self.client.status()
            self.assertEqual(request.call_count, 1, 'Health is cached to avoid repeated probes')
            self.assertTrue(request.call_args.args[0].full_url.endswith('/health'))

    def test_status_accepts_authenticated_proxy_without_api_key(self):
        with patch.object(self.client.opener, 'open', return_value=io.BytesIO(b'{"authenticated":true}')):
            self.assertTrue(self.client.status()['hermes_auth_configured'])

    def test_status_failure_is_not_reported_as_ready(self):
        with patch.object(self.client.opener, 'open', side_effect=OSError):
            self.assertFalse(self.client.status()['hermes_reachable'])
            self.assertFalse(self.client.status()['hermes_auth_configured'])


if __name__ == '__main__':
    unittest.main()
