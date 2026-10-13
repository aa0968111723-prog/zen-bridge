import io
import json
import unittest
from unittest.mock import patch
from app.desktop_llm import HYBRID_LOCAL_BUDGET_S, TRANSLATION_BUDGET_S, DesktopTranslator, loopback_url


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


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
            self.assertEqual(request.call_args.kwargs['timeout'], TRANSLATION_BUDGET_S)

    def test_local_context_keeps_current_utterance_intact_and_last(self):
        current='請保持當前這一句完整。'
        original=json.dumps({'text':current,'activity':{'notes':'參考資料'*4000},
                             'confirmedExamples':[{'zh':'舊句','en':'Old line'}]*20},ensure_ascii=False)
        with patch.object(self.client,'_request',return_value={'message':{'content':'Keep this sentence intact.'}}) as request:
            self.client.translate('Translate only',original,'local')
        payload=request.call_args.args[2]
        content=payload['messages'][1]['content']
        parsed=json.loads(content)
        self.assertEqual(parsed['text'],current)
        self.assertEqual(list(parsed)[-1],'text')
        self.assertLessEqual(len(content.encode())+len('Translate only'.encode()),7000)
        self.assertEqual(payload['options']['num_ctx'],8192)

    def test_oversized_current_utterance_never_silently_truncates(self):
        with patch.object(self.client,'_request') as request:
            with self.assertRaises(RuntimeError):
                self.client.translate('Translate',json.dumps({'text':'甲'*3000},ensure_ascii=False),'local')
        request.assert_not_called()

    def test_cloud_keeps_full_reference_context(self):
        original=json.dumps({'text':'新句','activity':{'notes':'参考'*4000}},ensure_ascii=False)
        response={'choices':[{'message':{'content':'A new sentence'},'finish_reason':'stop'}]}
        with patch.object(self.client,'_request',return_value=response) as request:
            self.client.translate('Translate',original,'cloud')
        self.assertEqual(request.call_args.args[2]['messages'][1]['content'],original)

    def test_cloud_mode_never_calls_local(self):
        result = {'choices': [{'message': {'content': 'translated'}, 'finish_reason': 'stop'}]}
        with patch.object(self.client, '_request', return_value=result) as request:
            self.assertEqual(self.client.translate('Translate', 'hello', 'cloud')['route'], 'cloud')
            self.assertEqual(request.call_args.args[0], self.client.hermes)

    def test_cloud_provider_rejecting_tool_choice_without_tools_can_translate(self):
        def strict_provider(base,path,payload,authenticated=False,timeout=None):
            if 'tool_choice' in payload and not payload.get('tools'):
                raise ValueError('HTTP 400: tool_choice requires tools')
            return {'choices':[{'message':{'content':'Bring your attention back to your breathing.'},'finish_reason':'stop'}]}
        with patch.object(self.client,'_request',side_effect=strict_provider):
            result=self.client.translate('Translate Chinese into English.','請將注意力帶回呼吸。','cloud')
        self.assertEqual(result['route'],'cloud')
        self.assertIn('breathing',result['text'])

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

    def test_missing_usage_is_not_reported_as_free(self):
        with patch.object(self.client, '_request', return_value={'message': {'content': 'translated'}}):
            result = self.client.translate('Translate', 'hello', 'local')
        self.assertFalse(result['usage_known'])
        self.assertEqual(result['cost_note'], '沒有上游用量，不能當成免費')
        self.assertFalse(self.client.status()['cost']['known'])
        self.assertIsNone(self.client.status()['cost']['prompt_tokens'])

    def test_reported_usage_is_kept(self):
        response = {'message': {'content': 'translated'}, 'prompt_eval_count': 4, 'eval_count': 2}
        with patch.object(self.client, '_request', return_value=response):
            result = self.client.translate('Translate', 'hello', 'local')
        self.assertTrue(result['usage_known'])
        self.assertEqual(result['prompt_tokens'], 4)
        self.assertEqual(self.client.cost_status()['completion_tokens'], 2)

    def test_slow_local_failure_still_tries_cloud_with_remaining_timeout(self):
        clock = _Clock()
        client = DesktopTranslator(clock=clock)
        seen = []

        def fake_provider(base, path, payload, authenticated=False, timeout=None):
            seen.append((base, timeout))
            if base == client.local:
                clock.now += timeout
                raise TimeoutError('slow local')
            return {'choices': [{'message': {'content': 'from cloud'}, 'finish_reason': 'stop'}]}

        with patch.object(client, '_request', side_effect=fake_provider):
            result = client.translate('Translate', 'hello', 'hybrid')
        self.assertEqual(result['route'], 'cloud')
        self.assertTrue(result['fallback'])
        self.assertEqual(seen[0], (client.local, HYBRID_LOCAL_BUDGET_S))
        self.assertEqual(seen[1], (client.hermes, TRANSLATION_BUDGET_S - HYBRID_LOCAL_BUDGET_S))
        self.assertEqual(len(seen), 2)

    def test_hybrid_local_success_does_not_call_cloud(self):
        clock = _Clock()
        client = DesktopTranslator(clock=clock)
        seen = []

        def fake_provider(base, path, payload, authenticated=False, timeout=None):
            seen.append((base, timeout))
            clock.now += 1
            return {'message': {'content': 'from local'}}

        with patch.object(client, '_request', side_effect=fake_provider):
            result = client.translate('Translate', 'hello', 'hybrid')
        self.assertEqual(result['route'], 'local')
        self.assertFalse(result['fallback'])
        self.assertEqual(seen, [(client.local, HYBRID_LOCAL_BUDGET_S)])

    def test_exhausted_budget_does_not_call_the_next_route(self):
        clock = _Clock()
        client = DesktopTranslator(clock=clock)
        seen = []

        def fake_provider(base, path, payload, authenticated=False, timeout=None):
            seen.append(base)
            clock.now += TRANSLATION_BUDGET_S
            raise TimeoutError('budget spent')

        with patch.object(client, '_request', side_effect=fake_provider), self.assertRaises(RuntimeError):
            client.translate('Translate', 'hello', 'hybrid')
        self.assertEqual(seen, [client.local])

    def test_single_mode_keeps_the_full_budget_on_its_own_route(self):
        clock = _Clock()
        client = DesktopTranslator(clock=clock)
        seen = []

        def fake_provider(base, path, payload, authenticated=False, timeout=None):
            seen.append((base, timeout))
            if base == client.local:
                raise TimeoutError('local down')
            return {'choices': [{'message': {'content': 'cloud'}, 'finish_reason': 'stop'}]}

        with patch.object(client, '_request', side_effect=fake_provider):
            self.assertEqual(client.translate('Translate', 'hello', 'cloud')['route'], 'cloud')
        self.assertEqual(seen, [(client.hermes, TRANSLATION_BUDGET_S)])


def test_metrics_file_survives_a_new_instance(tmp_path):
    from app.agent_metrics import AgentMetrics
    path = tmp_path / "agent-metrics.jsonl"
    first = AgentMetrics(path=path)
    first.record("translate", 12, True, route="local", usage_known=False)
    second = AgentMetrics(path=path)
    snap = second.snapshot()
    assert snap["persisted_completed"] == 1
    assert snap["cost_known"] is False
    assert "不能把 0 當成免費" in snap["note"]
    assert "hello" not in path.read_text(encoding="utf-8")

def test_translation_inherits_remaining_queue_budget():
    from app.desktop_llm import TRANSLATION_DEADLINE
    client=DesktopTranslator(clock=lambda:40)
    token=TRANSLATION_DEADLINE.set(55)
    try:
        with patch.object(client,'_request',return_value={'choices':[{'message':{'content':'Hello'},'finish_reason':'stop'}]}) as request:
            client.translate('Translate','hello','cloud')
        assert request.call_args.kwargs['timeout']==15
    finally:
        TRANSLATION_DEADLINE.reset(token)


if __name__ == '__main__':
    unittest.main()
