from __future__ import annotations

import unicodedata

import pytest

from app.ja_linebreak import break_ja_ex


_SIMPLIFIED_ONLY = frozenset(
    "这们说为头门车东长书爱个请现开还过进远见时该对无应实业专丰丽举义习乡买乱争于亏云亚产亩亲仅从仓仪价众优伞伟传伤伦伪侦侧侨俭债倾偿储兑党兰兴养冈册写军农决减凭击凿则刚创删别剂剑剥剧劝办务动励劳势勋匀区华协单卖卢卫厂厅历压厌厕县双发变叙叹吗启员呛呜咏咙响哑唤啧喷嘱园围团圆场坏坚坛坞坟坠垄垒垦垫埚堑墙壳壶处备复够夺奋奖妆娄娅娆娇娱婴婵孙宁审宪宫宽宾寝寻导尔尘尝尧层届岁岗岚岭峡峦巩币师帐帘带帮并庆庄库庙废异弃张弯强归录彻忆忧怀态总恳恶悦惊惨惩惫惯愤懒戏户执扩扫扬扰抚抛拟拥拦拨择挚换掷掺搀搁搂搅摄摇撑撵擞攒昼显暂极构枪枫柜标栈栏树样档桥桦检榄槛歼毙毡气汇汉污汤汹沟沥沦沧沪泞洁洒洼浅浆浇浊测济浓涛涝涟涡涣涤润涧涨渊渔溃滚满滤滥滨滩澜灭灵灾灿炼烁烂烛烟烦烧烫爷牵狮狱猎珑琐琼瑶电畅疆疟疡疮疯盖盘矫矿码种积秽稳窃窑窜窝窥竖笔笼签简篮篱粮约红纤级纪纯纲纳纵纷纸纹纺纽线练绅细织终绊绍绎经绑绒结绘给络绝绞统继绩绪续维绵绸综绽缁缂缃缄缅缆缉缎编缚缝缠缨缭缮缰缴网罗罚罢翘耸聂聋职联肃肠肤肾肿胀胁胜胧胶脉脏脑脓脸腊腻腾舰舱艺节芜苏茧荡荣荤荫药莹营萧萨蔼蕴虏虑虾蚀蚁蚂蛊蜡蝇补袄袜裆裤观规觉计订认讥让训议讯记讲讳讴讶许论讼讽设访诀证评诅识诈诉诊译试诗诚诛话诞诡询该详诧诬语误诱诲诸诺读谁调谅谆谈谊谋谍谐谑谓谗谛谜谢谣谦谧谨谩谪谬谭谱谴贡财贤败账货贩贪贫贬购贮贯贴贵贺贻贼贾贿赁赂赃资赊赋赌赎赏赐赔赖赘赚赛赞赠赵赶跃践踊踌躯轧轨轩转轮软轰轻载较辅辆辊辋辍辎辐辖辗辘边辽迁运违迟选逊逻递释针钉钓钙钝钞钟钠钢钥钦钧钩钻钾铀铁铂铃铅铆铐铭铛铜铝铡铣铨铩铬铮铰铲铳铵银铺铸链销锁锄锅锈锋锌锐锑锒锔锕错锚锛锝锞锟锡锣锤锥锦锨锭键锯锰锲锵锷锹锻镀镁镂镇镊镍镏镐镑镒镓镖镗镙镜镝镞镣镧镨镪镫镭镯镰镶闪闭问闯闰闲间闷闸闹闺闻闽阀阁阂阅阉阎阐阑阔队阳阴阵阶际陆陈陕陨险隐难雏雾顶项顺须顽顾顿颁颂预颅领颇颈颊颌频颓颔颖颗题颜额颠颤风飘飞饥饭饮饯饰饱饲饵饷饺饼饿馁馄馅馆馈馋馍馏馒驳驴驶驷驸驻驼驽驾驿骁骂骄骅骆骇骈骋验骏骗骚骡骢骤骥髅鱼鲁鲂鲅鲆鲇鲈鲋鲍鲎鲐鲑鲒鲔鲕鲖鲗鲙鲚鲛鲞鲟鲠鲡鲢鲣鲤鲥鲦鲧鲨鲩鲫鲭鲮鲰鲱鲲鲳鲴鲵鲶鲷鲸鲺鲻鲼鲽鲿鳃鳄鳅鳆鳇鳈鳉鳊鳋鳌鳍鳎鳏鳐鳑鳒鳓鳔鳕鳖鳗鳙鳜鳝鳞鳟鳢鸟鸡鸣鸥鸦鸫鸬鸭鸯鸱鸲鸳鸵鸶鸷鸸鸺鸽鸾鸿鹁鹂鹃鹄鹅鹆鹇鹤鹊鹋鹌鹍鹎鹏鹐鹑鹒鹔鹕鹖鹗鹘鹙鹚鹛鹜鹞鹟鹡鹢鹣鹦鹧鹨鹩鹪鹫鹬鹭鹰鹳麦麸黩黾鼋齐齿龀龁龃龄龅龆龇龈龉龊龋龌龙龚龛龟"
)
# These Simplified forms are also Japanese kanji and must not trigger the denylist.
_SIMPLIFIED_ONLY = _SIMPLIFIED_ONLY.difference("争写励嘱昼浅践云剥")
_CHINESE_ONLY = _SIMPLIFIED_ONLY | frozenset("，：；這們說")
_JAPANESE_ENDINGS = frozenset("。？！」』）】〉》〕〗〙〛")

_HANDWRITTEN_MT_OUTPUTS = (
    ("今天是晴天，我们去公园吧。", "今日は晴れです。公園へ行きましょう！"),
    ("请现在开始。", "それでは、始めましょう。"),
    ("不要担心，我们会解决这个问题。", "心配しないでください。こちらで解決します。"),
    ("去东京。", "東京へ行きます。"),
    ("这是个人意见。", "個人の意見です。"),
    ("坐电车来。", "電車で来ました。"),
)


def _chinese_only_leftovers(text: str) -> list[str]:
    """Find known Chinese-only punctuation and characters in a Japanese caption."""
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


@pytest.mark.parametrize(("source", "caption"), _HANDWRITTEN_MT_OUTPUTS)
def test_handwritten_zh_to_ja_outputs_meet_caption_quality(
    source: str, caption: str
) -> None:
    assert source
    _assert_caption_quality(caption)


@pytest.mark.parametrize(
    ("caption", "leftover"),
    [
        ("今日は，始めます。", "，"),
        ("これは重要：確認してください。", "："),
        ("这是良い機会です。", "这"),
        ("请现在开始。", "请"),
        ("說明を始めます。", "說"),
    ],
)
def test_chinese_only_characters_and_punctuation_are_detected(
    caption: str, leftover: str
) -> None:
    assert leftover in _chinese_only_leftovers(caption)
    with pytest.raises(AssertionError, match="Chinese-only leftovers"):
        _assert_caption_quality(caption)


@pytest.mark.parametrize("leftover", sorted(_CHINESE_ONLY))
def test_every_chinese_only_inventory_entry_is_rejected(leftover: str) -> None:
    caption = f"{leftover}日本語。"
    assert leftover in _chinese_only_leftovers(caption)
    with pytest.raises(AssertionError, match="Chinese-only leftovers"):
        _assert_caption_quality(caption)


@pytest.mark.parametrize("caption", ["今日は晴れです", "始めましょう,"])
def test_caption_without_japanese_sentence_ending_is_rejected(caption: str) -> None:
    with pytest.raises(AssertionError, match="Japanese sentence punctuation"):
        _assert_caption_quality(caption)


def test_display_line_over_16_fullwidth_columns_is_rejected() -> None:
    caption = "ア" * 17 + "。"
    result = break_ja_ex(caption, max_chars=16, max_lines=len(caption))
    assert _fullwidth_columns(result.lines[0]) == 18
    # Kinsoku keeps 。 with the 17-column katakana token, producing an 18-column line.
    with pytest.raises(AssertionError, match="display line exceeds 16"):
        _assert_caption_quality(caption)


def test_line_break_api_splits_long_captions_into_display_lines() -> None:
    caption = "今日はとても良い天気なので散歩に出かけましょう。"
    result = break_ja_ex(caption, max_chars=16, max_lines=len(caption))

    assert not result.overflow
    assert "".join(result.lines) == caption
    assert len(result.lines) > 1
    assert all(_fullwidth_columns(line) <= 16 for line in result.lines)
