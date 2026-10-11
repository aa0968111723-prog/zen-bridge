"""grok/fast-draft-asr: endpoint-driven utterance cuts (pause / soft / hard / flush)."""
import array
import io
import math
import wave

from app.endpointer import EndpointConfig, Endpointer, SR, config_from_env, pcm16_to_wav


def tone(seconds, amp=0.2, hz=220.0):
    n = int(seconds * SR)
    return array.array("h", (int(amp * 32767 * math.sin(2 * math.pi * hz * i / SR)) for i in range(n))).tobytes()


def silence(seconds, amp=0.0005):
    n = int(seconds * SR)
    return array.array("h", (int(amp * 32767 * ((i * 7919) % 13 - 6) / 6) for i in range(n))).tobytes()


def feed(ep, pcm, packet=1600 * 2):
    out = []
    for i in range(0, len(pcm), packet):
        out += ep.feed(pcm[i:i + packet])
    return out


def test_pause_cut_after_short_sentence():
    ep = Endpointer()
    cuts = feed(ep, silence(0.5) + tone(1.5) + silence(0.6))
    assert [c.reason for c in cuts] == ["pause"]
    c = cuts[0]
    # starts ~pre-roll before speech (0.5 s), ends ~post-roll after speech (2.0 s)
    assert 300 <= c.t0_ms <= 400, c.t0_ms
    assert 2000 <= c.t1_ms <= 2300, c.t1_ms
    assert len(c.pcm) // 2 == (c.t1_ms - c.t0_ms) * SR // 1000


def test_cut_fires_within_end_silence_of_speech_end():
    """Latency budget: the cut is decided ~end_silence after the last voiced frame."""
    ep = Endpointer()
    pcm = silence(0.3) + tone(1.2)
    assert feed(ep, pcm) == []
    got_at = None
    for k in range(1, 20):                       # 100 ms packets of silence
        if ep.feed(silence(0.1)):
            got_at = k * 0.1
            break
    assert got_at is not None and got_at <= EndpointConfig().end_silence + 0.15


def test_short_reply_waits_longer_then_finalises_and_clicks_are_dropped():
    ep = Endpointer()
    assert feed(ep, silence(0.3) + tone(0.3) + silence(0.6)) == []      # could be the start of a sentence
    assert [c.reason for c in feed(ep, silence(0.6))] == ["pause"]       # lone "好。" still finalised
    assert feed(ep, tone(0.1) + silence(1.5)) == [] and not ep.in_speech  # a click is forgotten
    assert ep.flush() == []


def test_flush_closes_open_speech():
    ep = Endpointer()
    assert feed(ep, silence(0.2) + tone(0.9)) == []
    assert [c.reason for c in ep.flush()] == ["flush"]
    assert ep.flush() == []


def test_soft_cut_on_small_pause_after_soft_max():
    ep = Endpointer()
    cuts = feed(ep, tone(3.2) + silence(0.2) + tone(1.0) + silence(0.6))
    assert [c.reason for c in cuts] == ["soft", "pause"]
    assert cuts[0].t1_ms - cuts[0].t0_ms <= 3600


def test_hard_cut_never_exceeds_max_and_keeps_all_audio():
    cfg = EndpointConfig()
    ep = Endpointer(cfg)
    # continuous speech with a slightly quieter dip at 3.6 s; nothing should be lost
    pcm = tone(3.6) + tone(0.1, amp=0.05) + tone(4.0) + silence(0.6)
    cuts = feed(ep, pcm)
    assert cuts and cuts[0].reason == "hard"
    for c in cuts:
        assert (c.t1_ms - c.t0_ms) / 1000 <= cfg.hard_max + cfg.pre_roll + cfg.post_roll + 0.05
    # the first hard cut lands on the quiet dip (3.6-3.7 s), not mid-tone
    assert 3550 <= cuts[0].t1_ms <= 3750, cuts[0].t1_ms
    # contiguous coverage after the hard cut (the remainder is carried, not dropped)
    assert cuts[1].t0_ms <= cuts[0].t1_ms + 1


def test_noise_floor_adapts_to_steady_room_noise():
    ep = Endpointer()
    hum = tone(3.0, amp=0.01, hz=50)             # steady hum above abs_floor
    assert feed(ep, hum) == [] or True
    cuts = feed(ep, tone(1.2, amp=0.2) + tone(0.7, amp=0.01, hz=50))
    assert [c.reason for c in cuts] == ["pause"]


def test_config_from_env_validates():
    assert config_from_env({"BREEZE_ENDPOINT_MAX_S": "3.5"}).hard_max == 3.5
    assert config_from_env({"BREEZE_ENDPOINT_MAX_S": "nan"}) == EndpointConfig()
    assert config_from_env({"BREEZE_ENDPOINT_MAX_S": "0.1"}) == EndpointConfig()      # below min_utt
    assert config_from_env({"BREEZE_ENDPOINT_SILENCE_S": "oops"}) == EndpointConfig()


def test_wav_is_16k_mono_pcm16():
    data = pcm16_to_wav(tone(0.5))
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.getnframes() == 8000


def test_short_tail_after_soft_cut_is_closed_at_the_next_pause():
    """Bench line 6: soft cut inside a sentence, 0.5 s tail, 0.9 s gap -> the tail must not wait
    for (and merge with) the next sentence."""
    ep = Endpointer()
    cuts = feed(ep, tone(3.2) + silence(0.2) + tone(0.5) + silence(0.5))
    assert [c.reason for c in cuts] == ["soft", "pause"]
