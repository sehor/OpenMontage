import json
import wave
from copy import deepcopy
from unittest.mock import Mock

import pytest

from lib.dashscope_dialogue import compile_dialogue
from lib.dashscope_flash_render import render_flash
from lib.dashscope_alignment import align_chunk
from lib.dashscope_ssml import validate_ssml
from tools.base_tool import ToolResult
from tools.subtitle.subtitle_gen import SubtitleGen

MODEL = 'qwen-audio-3.1-tts-flash'
ROLES = {'A': {'voice_id': 'voice-a', 'model': MODEL, 'ssml_supported': True},
         'B': {'voice_id': 'voice-b', 'model': MODEL, 'ssml_supported': True}}


def plan(source='A：你好。\nB：好的。\nA：再见。\nB：谢谢。', **kwargs):
    return compile_dialogue(source, deepcopy(ROLES), model='flash', **kwargs)


def fake_tools(fail_at=None):
    calls = []
    def synth(inputs):
        calls.append(inputs)
        if len(calls) == fail_at:
            return ToolResult(success=False, error='timeout')
        with wave.open(inputs['output_path'], 'wb') as output:
            output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\x01\x00' * 24000)
        return ToolResult(success=True, data={'request_id': str(len(calls))})
    tts = Mock(execute=synth)
    # ASR script for deterministic one-second generated chunks.
    texts = iter(['你好。', '好的。', '再见。', '谢谢。'])
    def transcribe(inputs):
        return ToolResult(success=True, data={'segments': [{'words': [
            {'word': next(texts), 'start': 0.1, 'end': 0.9}]}]})
    asr = Mock(execute=Mock(side_effect=transcribe))
    # Mock does not represent provider status.
    del asr.get_status
    return tts, asr, calls


def test_flash_compiler_single_voice_and_no_references():
    compiled = plan()
    assert compiled['mode'] == 'segmented'
    assert len(compiled['chunks']) == 4
    for chunk in compiled['chunks']:
        request = chunk['synthesis_inputs']
        assert 'reference_paths' not in request
        assert validate_ssml(request['text'], MODEL, request['ssml_voice_models'])['voice_ids'] == [request['voice_id']]
        assert chunk['text']


def test_voice_capability_and_model_mismatch():
    roles = deepcopy(ROLES)
    roles['A'].pop('ssml_supported')
    plain = compile_dialogue('A：你好。', roles, model='flash')
    assert 'enable_ssml' not in plain['chunks'][0]['synthesis_inputs']
    with pytest.raises(ValueError, match='SSML support'):
        compile_dialogue('你好。', roles, [{'role': 'A', 'start': 0, 'end': 3, 'rate': 1.2}], 'plain', 'flash')
    roles['A']['model'] = 'next'
    with pytest.raises(ValueError, match='Voice model'):
        compile_dialogue('A：你好。', roles, model='flash')


def test_pronunciation_display_and_reading_are_distinct():
    source = 'W3C和重庆。'
    ssml = '<speak voice="voice-a"><sub alias="网络标准">W3C</sub>和<phoneme alphabet="py" ph="chong2 qing4">重庆</phoneme>。</speak>'
    compiled = plan(source, segments=[{'role': 'A', 'start': 0, 'end': len(source), 'ssml': ssml}], source_format='plain')
    chunk = compiled['chunks'][0]
    assert chunk['text'] == source
    assert ''.join(x['reading'] for x in chunk['reading_map']) == '网络标准和重庆。'
    cues = align_chunk(chunk, [{'words': [{'word': '网络标准和重庆', 'start': .1, 'end': .8}]}], 1)
    assert cues[0]['text'] == source
    with pytest.raises(ValueError):
        validate_ssml('<speak voice="voice-a"><phoneme alphabet="py" ph="chong2">重庆</phoneme></speak>', MODEL, {'voice-a': MODEL})
    with pytest.raises(ValueError):
        validate_ssml('<speak voice="voice-a"><sub alias="x"><break time="50ms"/></sub></speak>', MODEL, {'voice-a': MODEL})


def test_long_turn_sentence_split(monkeypatch):
    import lib.dashscope_flash as flash
    profile = flash.speech_model('flash').model_copy(update={'max_prompt_length': 70})
    monkeypatch.setattr(flash, 'speech_model', lambda *args: profile)
    text = '你好世界。' * 20
    compiled = plan('A：' + text)
    assert len(compiled['chunks']) > 1
    assert ''.join(c['text'] for c in compiled['chunks']) == text
    assert all(len(c['synthesis_inputs']['text']) <= 70 for c in compiled['chunks'])
    assert all(c['turn'] == 0 for c in compiled['chunks'])
    with pytest.raises(ValueError, match='Oversized'):
        plan('A：' + '字' * 100)


def test_four_turn_assembly_cache_gap_and_timeline(tmp_path):
    tts, asr, calls = fake_tools()
    compiled = plan()
    result = render_flash(compiled, tmp_path, tts, asr, SubtitleGen())
    directory = __import__('pathlib').Path(result['directory'])
    with wave.open(str(directory/'dialogue.wav')) as audio:
        assert audio.getnframes() == 4 * 24000 + 3 * 7200
    timeline = json.loads((directory/'timeline.json').read_text())
    assert [s['start_sample'] for s in timeline['segments']] == [0, 31200, 62400, 93600]
    captions = json.loads((directory/'subtitles.json').read_text())['cues']
    assert len(captions) == 4
    assert captions[1]['start'] == 1.4
    assert captions[-1]['end'] <= timeline['duration_seconds']
    assert len(calls) == 4
    # Spacing changes do not resubmit audio or rerun ASR.
    updated = render_flash(compiled, tmp_path, tts, asr, SubtitleGen(), gap_ms=100)
    assert updated['version'] != result['version']
    assert len(calls) == 4 and asr.execute.call_count == 4
    assert (tmp_path/'current.json').is_file()


def test_failure_preserves_chunks_and_blocks_unknown_retries(tmp_path):
    tts, asr, calls = fake_tools(fail_at=2)
    with pytest.raises(ValueError, match='TTS failed'):
        render_flash(plan(), tmp_path, tts, asr, SubtitleGen())
    assert not (tmp_path/'current.json').exists()
    assert len(list((tmp_path/'chunks').glob('*.wav'))) == 1
    with pytest.raises(ValueError, match='unknown outcome'):
        render_flash(plan(), tmp_path, tts, asr, SubtitleGen())
    assert len(calls) == 2


def test_bad_captions_only_rerun_alignment(tmp_path):
    tts, asr, calls = fake_tools()
    asr.execute = Mock(return_value=ToolResult(success=True, data={'segments': [
        {'words': [{'word': '错字', 'start': 0, 'end': 1}]}]}))
    with pytest.raises(ValueError, match='needs_review'):
        render_flash(plan('A：你好。'), tmp_path, tts, asr, SubtitleGen())
    assert len(calls) == 1
    assert list((tmp_path/'chunks').glob('*.review.json'))
    transcript = next((tmp_path/'chunks').glob('*.transcript.json'))
    transcript.write_text(json.dumps([{'words': [{'word': '你好', 'start': .1, 'end': .9}]}]))
    render_flash(plan('A：你好。'), tmp_path, tts, asr, SubtitleGen())
    assert len(calls) == 1


def test_missing_sentence_and_invalid_timestamps_are_not_estimated():
    chunk = plan('A：你好。再见。')['chunks'][0]
    with pytest.raises(ValueError, match='needs_review'):
        align_chunk(chunk, [{'words': [{'word': '你好', 'start': 0, 'end': 1}]}], 1)
    with pytest.raises(ValueError, match='timestamps'):
        align_chunk(chunk, [{'words': [{'word': '你好再见', 'start': 0, 'end': 2}]}], 1)


def test_edited_turn_reuses_unaffected_audio(tmp_path):
    tts, asr, calls = fake_tools()
    compiled = plan()
    render_flash(compiled, tmp_path, tts, asr, SubtitleGen())
    changed = plan('A：您好。\nB：好的。\nA：再见。\nB：谢谢。')
    asr.execute = Mock(return_value=ToolResult(success=True, data={'segments': [
        {'words': [{'word': '您好', 'start': .1, 'end': .9}]}]}))
    render_flash(changed, tmp_path, tts, asr, SubtitleGen())
    assert len(calls) == 5


def test_number_spellings_are_aligned_without_changing_display():
    chunk = plan('A：数字123。')['chunks'][0]
    for reading in ('数字一百二十三', '数字一二三', '数字123'):
        cues = align_chunk(chunk, [{'words': [{'word': reading, 'start': .1, 'end': .9}]}], 1)
        assert cues[0]['text'] == '数字123。'


def test_intra_turn_split_does_not_add_gap(tmp_path):
    compiled = plan('A：你好。\nA：好的。')
    compiled['chunks'][1]['turn'] = 0
    tts, asr, calls = fake_tools()
    result = render_flash(compiled, tmp_path, tts, asr, SubtitleGen())
    timeline = json.loads((__import__('pathlib').Path(result['directory'])/'timeline.json').read_text())
    assert timeline['duration_samples'] == 48000


def test_explicit_gap_and_unknown_receipt(tmp_path):
    compiled = plan('A：你好。\nB：好的。')
    compiled['chunks'][0]['gap_after_ms'] = 50
    tts, asr, _ = fake_tools()
    result = render_flash(compiled, tmp_path, tts, asr, SubtitleGen(), gap_ms=1000)
    timeline = json.loads((__import__('pathlib').Path(result['directory'])/'timeline.json').read_text())
    assert timeline['duration_samples'] == 49200
    receipt = next((tmp_path/'chunks').glob('*.receipt.json'))
    receipt.write_text(json.dumps({'status': 'request_started'}))
    with pytest.raises(ValueError, match='unknown outcome'):
        render_flash(compiled, tmp_path, tts, asr, SubtitleGen())


def test_library_voice_uses_id_without_reference(tmp_path):
    folder = tmp_path/'saved'
    folder.mkdir()
    (folder/'voice.json').write_text(json.dumps({'success': True, 'provider': 'dashscope',
        'model': MODEL, 'data': {'voice_id': MODEL + '-saved'}, 'source_path': 'missing.wav'}))
    compiled = compile_dialogue('A：你好。', {'A': 'saved'}, model='flash', library=tmp_path)
    assert compiled['chunks'][0]['synthesis_inputs']['voice_id'] == MODEL + '-saved'


def test_cache_corruption_is_not_resubmitted(tmp_path):
    tts, asr, calls = fake_tools()
    render_flash(plan('A：你好。'), tmp_path, tts, asr, SubtitleGen())
    next((tmp_path/'chunks').glob('*.wav')).write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='missing or changed'):
        render_flash(plan('A：你好。'), tmp_path, tts, asr, SubtitleGen())
    assert len(calls) == 1


def test_traditional_transcription_preserves_simplified_source():
    pytest.importorskip('opencc')
    chunk = plan('A：音频合并以后，字幕应该同步。')['chunks'][0]
    cues = align_chunk(chunk, [{'words': [{'word': '音頻合併以後字幕應該同步', 'start': .1, 'end': .9}]}], 1)
    assert cues[0]['text'] == chunk['text']


def test_long_dialogue_sample_positions_do_not_drift(tmp_path):
    compiled = plan()
    seed = deepcopy(compiled['chunks'])
    compiled['chunks'] = []
    for i in range(200):
        chunk = deepcopy(seed[i % 4])
        chunk.update(turn=i, start=i*10, end=i*10+len(chunk['text']))
        compiled['chunks'].append(chunk)
    tts, asr, calls = fake_tools()
    result = render_flash(compiled, tmp_path, tts, asr, SubtitleGen(), gap_ms=333)
    directory = __import__('pathlib').Path(result['directory'])
    timeline = json.loads((directory/'timeline.json').read_text())
    assert timeline['duration_samples'] == 200*24000 + 199*7992
    assert timeline['segments'][-1]['start_sample'] == 199*(24000+7992)
    assert len(calls) == 4
