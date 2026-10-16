"""Extra Japanese caption-quality cases beyond test_ja_caption_quality.py.

Uses handwritten / fake MT outputs only (no network, no paid APIs).
Asserts: no Simplified/Traditional Chinese-only leftovers, Japanese
sentence-end punctuation, and full-width display width ≤16 per line via
``app.ja_linebreak.break_ja_ex``.
"""
from __future__ import annotations

import unicodedata

import pytest

from app.ja_linebreak import break_ja_ex


# Same denylist as test_ja_caption_quality.py (kept local so extras stay
# self-contained and do not import sibling test modules).
_SIMPLIFIED_ONLY = frozenset(
    "这们说为头门车东长书爱个请现开还过进远见时该对无应实业专丰丽举义习乡买乱争于亏云亚产亩亲仅从仓仪价众优伞伟传伤伦伪侦侧侨俭债倾偿储兑党兰兴养冈册写军农决减凭击凿则刚创删别剂剑剥剧劝办务动励劳势勋匀区华协单卖卢卫厂厅历压厌厕县双发变叙叹吗启员呛呜咏咙响哑唤啧喷嘱园围团圆场坏坚坛坞坟坠垄垒垦垫埚堑墙壳壶处备复够夺奋奖妆娄娅娆娇娱婴婵孙宁审宪宫宽宾寝寻导尔尘尝尧层届岁岗岚岭峡峦巩币师帐帘带帮并庆庄库庙废异弃张弯强归录彻忆忧怀态总恳恶悦惊惨惩惫惯愤懒戏户执扩扫扬扰抚抛拟拥拦拨择挚换掷掺搀搁搂搅摄摇撑撵擞攒昼显暂极构枪枫柜标栈栏树样档桥桦检榄槛歼毙毡气汇汉污汤汹沟沥沦沧沪泞洁洒洼浅浆浇浊测济浓涛涝涟涡涣涤润涧涨渊渔溃滚满滤滥滨滩澜灭灵灾灿炼烁烂烛烟烦烧烫爷牵狮狱猎珑琐琼瑶电畅疆疟疡疮疯盖盘矫矿码种积秽稳窃窑窜窝窥竖笔笼签简篮篱粮约红纤级纪纯纲纳纵纷纸纹纺纽线练绅细织终绊绍绎经绑绒结绘给络绝绞统继绩绪续维绵绸综绽缁缂缃缄缅缆缉缎编缚缝缠缨缭缮缰缴网罗罚罢翘耸聂聋职联肃肠肤肾肿胀胁胜胧胶脉脏脑脓脸腊腻腾舰舱艺节芜苏茧荡荣荤荫药莹营萧萨蔼蕴虏虑虾蚀蚁蚂蛊蜡蝇补袄袜裆裤观规觉计订认讥让训议讯记讲讳讴讶许论讼讽设访诀证评诅识诈诉诊译试诗诚诛话诞诡询该详诧诬语误诱诲诸诺读谁调谅谆谈谊谋谍谐谑谓谗谛谜谢谣谦谧谨谩谪谬谭谱谴贡财贤败账货贩贪贫贬购贮贯贴贵贺贻贼贾贿赁赂赃资赊赋赌赎赏赐赔赖赘赚赛赞赠赵赶跃践踊踌躯轧轨轩转轮软轰轻载较辅辆辊辋辍辎辐辖辗辘边辽迁运违迟选逊逻递释针钉钓钙钝钞钟钠钢钥钦钧钩钻钾铀铁铂铃铅铆铐铭铛铜铝铡铣铨铩铬铮铰铲铳铵银铺铸链销锁锄锅锈锋锌锐锑锒锔锕错锚锛锝锞锟锡锣锤锥锦锨锭键锯锰锲锵锷锹锻镀镁镂镇镊镍镏镐镑镒镓镖镗镙镜镝镞镣镧镨镪镫镭镯镰镶闪闭问闯闰闲间闷闸闹闺闻闽阀阁阂阅阉阎阐阑阔队阳阴阵阶际陆陈陕陨险隐难雏雾顶项顺须顽顾顿颁颂预颅领颇颈颊颌频颓颔颖颗题颜额颠颤风飘飞饥饭饮饯饰饱饲饵饷饺饼饿馁馄馅馆馈馋馍馏馒驳驴驶驷驸驻驼驽驾驿骁骂骄骅骆骇骈骋验骏骗骚骡骢骤骥髅鱼鲁鲂鲅鲆鲇鲈鲋鲍鲎鲐鲑鲒鲔鲕鲖鲗鲙鲚鲛鲞鲟鲠鲡鲢鲣鲤鲥鲦鲧鲨鲩鲫鲭鲮鲰鲱鲲鲳鲴鲵鲶鲷鲸鲺鲻鲼鲽鲿鳃鳄鳅鳆鳇鳈鳉鳊鳋鳌鳍鳎鳏鳐鳑鳒鳓鳔鳕鳖鳗鳙鳜鳝鳞鳟鳢鸟鸡鸣鸥鸦鸫鸬鸭鸯鸱鸲鸳鸵鸶鸷鸸鸺鸽鸾鸿鹁鹂鹃鹄鹅鹆鹇鹤鹊鹋鹌鹍鹎鹏鹐鹑鹒鹔鹕鹖鹗鹘鹙鹚鹛鹜鹞鹟鹡鹢鹣鹦鹧鹨鹩鹪鹫鹬鹭鹰鹳麦麸黩黾鼋齐齿龀龁龃龄龅龆龇龈龉龊龋龌龙龚龛龟"
)
_SIMPLIFIED_ONLY = _SIMPLIFIED_ONLY.difference("争写励嘱昼浅践云剥")
_CHINESE_ONLY = _SIMPLIFIED_ONLY | frozenset("，：；這們說")
_JAPANESE_ENDINGS = frozenset("。？！」』）】〉》〕〗〙〛")

# Extra handwritten / fake MT pairs not in the base suite.
_EXTRA_HANDWRITTEN_MT_OUTPUTS = (
    ("今天天气很好，我们去散步吧。", "今日はとても良い天気なので、散歩に行きましょう。"),
    ("请打开麦克风。", "マイクをオンにしてください。"),
    ("不要忘记带帽子。", "帽子を忘れないでください。"),
    ("下课后见。", "授業の後で会いましょう。"),
    ("这个讲座很重要。", "この講義はとても重要です。"),
    # Half-width katakana is valid Japanese (display half-col); no Chinese leftovers.
    ("用片假名写咖啡。", "ｶﾌｪへ行きましょう。"),
    # Mixed full-width Japanese punctuation (、。！？).
    ("真的吗？快点！", "本当ですか？早く！"),
    # Closing-bracket sentence end.
    ("他说「明天见」。", "彼は「また明日」と言いました。"),
    # Shared-form kanji that look Simplified but are Japanese (争/写/… allowed).
    ("不要争论。", "争わないでください。"),
    ("请写下笔记。", "ノートに書いてください。"),
)

# Long captions that must wrap via break_ja_ex without overflow at ≤16 fw cols.
_LONG_CAPTIONS_NEEDING_BREAK = (
    "今日は淡江大学の禅学社で特別な講義がありますので早めに会場へ来てください。",
    "マイクの音量が小さすぎるときはホストに連絡して調整してもらいましょう。",
    "参加者は全員、入口で名札を受け取ってからE310の教室に入ってください。",
)


def _chinese_only_leftovers(text: str) -> list[str]:
    return sorted(set(text) & _CHINESE_ONLY)


def _fullwidth_columns(text: str) -> float:
    return sum(
        1.0 if unicodedata.east_asian_width(char) in {"F", "W"} else 0.5
        for char in text
    )


def _assert_caption_quality(caption: str) -> None:
    leftovers = _chinese_only_leftovers(caption)
    assert not leftovers, f"Chinese-only leftovers: {leftovers!r}"
    stripped = caption.rstrip()
    assert stripped and stripped[-1] in _JAPANESE_ENDINGS, (
        "caption must end with Japanese sentence punctuation or a closing bracket"
    )
    result = break_ja_ex(
        caption, max_chars=16, max_lines=max(1, len(caption))
    )
    assert not result.overflow
    assert "".join(result.lines) == caption
    assert all(_fullwidth_columns(line) <= 16 for line in result.lines), (
        f"display line exceeds 16 full-width columns: {result.lines!r}"
    )


@pytest.mark.parametrize(("source", "caption"), _EXTRA_HANDWRITTEN_MT_OUTPUTS)
def test_extra_handwritten_zh_to_ja_outputs_meet_caption_quality(
    source: str, caption: str
) -> None:
    assert source
    _assert_caption_quality(caption)


@pytest.mark.parametrize(
    ("caption", "leftover"),
    [
        # Half-width katakana caption polluted with Simplified leftovers.
        ("ｶﾌｪで这を注文します。", "这"),
        ("ｲﾍﾞﾝﾄは请参加してください。", "请"),
        # Mixed full-width Chinese punct amid otherwise Japanese text.
        ("開始します；準備してください。", "；"),
        ("説明：これは重要です。", "："),
        ("今日は，晴れです。", "，"),
        # Traditional Chinese-only leftovers.
        ("這は良い機会です。", "這"),
        ("們說は来ました。", "們"),
        ("說を始めます。", "說"),
    ],
)
def test_chinese_leftovers_with_halfwidth_kana_or_mixed_punct_detected(
    caption: str, leftover: str
) -> None:
    assert leftover in _chinese_only_leftovers(caption)
    with pytest.raises(AssertionError, match="Chinese-only leftovers"):
        _assert_caption_quality(caption)


@pytest.mark.parametrize("caption", _LONG_CAPTIONS_NEEDING_BREAK)
def test_long_fake_mt_captions_break_within_16_fullwidth_columns(
    caption: str,
) -> None:
    result = break_ja_ex(caption, max_chars=16, max_lines=len(caption))
    assert not result.overflow
    assert "".join(result.lines) == caption
    assert len(result.lines) > 1
    assert all(_fullwidth_columns(line) <= 16 for line in result.lines)
    _assert_caption_quality(caption)


@pytest.mark.parametrize(
    "caption",
    [
        "本当ですか？",
        "早く来てください！",
        "彼は「また明日」と言いました。",
        "（注意）音量を下げてください。",
        "【重要】マイクをオンにしてください。",
    ],
)
def test_mixed_fullwidth_japanese_punctuation_endings_accepted(
    caption: str,
) -> None:
    _assert_caption_quality(caption)


@pytest.mark.parametrize("caption", ["", "   ", "\t", "\n", "　"])
def test_empty_or_whitespace_captions_are_rejected(caption: str) -> None:
    with pytest.raises(AssertionError, match="Japanese sentence punctuation"):
        _assert_caption_quality(caption)


@pytest.mark.parametrize(
    "caption",
    [
        "始めましょう.",  # ASCII period
        "始めましょう、",  # enumeration comma is not a sentence ending
        "始めましょう・",  # middle dot
        "始めましょう…",  # ellipsis
    ],
)
def test_non_sentence_end_punctuation_is_rejected(caption: str) -> None:
    with pytest.raises(AssertionError, match="Japanese sentence punctuation"):
        _assert_caption_quality(caption)


def test_halfwidth_katakana_mixed_with_particles_breaks_within_budget() -> None:
    # Half-width katakana counts as 0.5 col; particles/punctuation allow wraps.
    caption = "ｶﾌｪへ行きましょう。ｲﾍﾞﾝﾄは五時からです。"
    result = break_ja_ex(caption, max_chars=16, max_lines=len(caption))
    assert not result.overflow
    assert "".join(result.lines) == caption
    assert len(result.lines) > 1
    assert all(_fullwidth_columns(line) <= 16 for line in result.lines)
    _assert_caption_quality(caption)


def test_unbreakable_halfwidth_katakana_run_over_budget_is_rejected() -> None:
    # One continuous half-width katakana token cannot split; kinsoku keeps 。,
    # so the display line exceeds 16 full-width columns and quality fails.
    caption = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ。"
    assert _fullwidth_columns(caption) > 16
    result = break_ja_ex(caption, max_chars=16, max_lines=len(caption))
    assert len(result.lines) == 1
    assert _fullwidth_columns(result.lines[0]) > 16
    with pytest.raises(AssertionError, match="display line exceeds 16"):
        _assert_caption_quality(caption)


def test_ruby_markup_captions_meet_quality_when_surface_is_clean() -> None:
    # Ruby annotations are kept by break_ja_ex; surface text must still be
    # Chinese-free and end with Japanese sentence punctuation.
    caption = "{禅|ぜん}学社へようこそ。"
    result = break_ja_ex(caption, max_chars=16, max_lines=len(caption))
    assert not result.overflow
    assert "".join(result.lines) == caption
    _assert_caption_quality(caption)


def test_ruby_markup_with_chinese_leftover_is_rejected() -> None:
    caption = "{禅|ぜん}学社へ这こそようこそ。"
    assert "这" in _chinese_only_leftovers(caption)
    with pytest.raises(AssertionError, match="Chinese-only leftovers"):
        _assert_caption_quality(caption)


def test_shared_form_kanji_exempt_from_simplified_denylist() -> None:
    # These forms appear in Simplified lists but are also Japanese kanji.
    for char in "争写励嘱昼浅践云剥":
        caption = f"{char}は日本語です。"
        assert char not in _chinese_only_leftovers(caption)
        _assert_caption_quality(caption)


def test_kinsoku_overflow_line_still_rejected_when_over_16() -> None:
    # Unbreakable fullwidth token + 。 can exceed 16 via kinsoku (same idea as
    # base suite, but with a different length to keep extras independent).
    caption = "イ" * 16 + "。"
    result = break_ja_ex(caption, max_chars=16, max_lines=len(caption))
    assert _fullwidth_columns(result.lines[0]) > 16
    with pytest.raises(AssertionError, match="display line exceeds 16"):
        _assert_caption_quality(caption)