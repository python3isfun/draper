"""A complete, hard-check-passing sample built from the spec's example seed.

Used by FixtureModelClient so the whole pipeline and API run without a real model, and by
tests as a known-good baseline to mutate.

Story content is in zh-CN because it is the product output language for this seed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import (
    AssetRequirement,
    Beat,
    BeatOutline,
    Character,
    CharacterLook,
    CharacterLookRef,
    CreativeBrief,
    DialogueCue,
    DialogueLine,
    DialogueScheduleEntry,
    Episode,
    EpisodeTiming,
    JobRequest,
    Location,
    LocationState,
    Prop,
    PropState,
    PropStateRef,
    Scene,
    SceneState,
    Shot,
    SourceBrief,
    SourceCoverage,
    StoryBible,
    StyleBrief,
)
from ..models.design_sheets import (
    CastLook,
    CastMember,
    CastSheet,
    PropItem,
    PropSheet,
    PropVariant,
    SetLocation,
    SetSheet,
    SetState,
)

SAMPLE_SEED = "一个即将失去店铺的年轻人，发现前来收租的老人是失踪多年的父亲。"
SHOT_MS = 15_000
MS_PER_CHAR = 250  # Text-stage speech-rate estimate; not a measured voice track.


def sample_request() -> JobRequest:
    return JobRequest.model_validate(
        {
            "sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}],
            "options": {"language": "zh-CN"},
        }
    )


def sample_source_brief(request: JobRequest | None = None) -> SourceBrief:
    request = request or sample_request()
    return SourceBrief(
        language=request.options.language or "zh-CN",
        observed_facts=["主角即将失去店铺", "收租老人是主角失踪多年的父亲"],
        user_constraints=["三集，每集120秒，每集2-3场、3-5人"],
        creative_assumptions=[
            "主角为女性店主林晓，经营一家旧书店",
            "父亲失踪原因与母亲当年的医药债务有关",
            "开发商经理赵经理为外部压力来源",
        ],
        source_coverage=[
            SourceCoverage(
                source_index=i, analyzed_sections=["full text"], notes="single sentence seed"
            )
            for i in range(len(request.sources))
        ],
        creative_brief=CreativeBrief(
            genre="家庭情感",
            audience_assumption="25-45岁短剧观众",
            core_conflict="保住店铺 vs. 面对抛家多年的父亲",
            emotional_experience="怀疑 → 愤怒 → 和解前的拉扯",
            reusable_assets=["书店", "父亲出租屋", "旧全家福"],
        ),
    )


def sample_story_bible() -> StoryBible:
    return StoryBible(
        title="最后一个月房租",
        logline="即将失去书店的林晓，发现来收最后一个月房租的房东是失踪十五年的父亲。",
        genre="家庭情感",
        audience_assumption="25-45岁短剧观众",
        emotional_goal="从怀疑到愿意并肩",
        core_conflict="店铺被开发商收走的期限 vs. 父女十五年的隔阂",
        series_synopsis="第一集认出伤疤；第二集父亲否认、委托书出现；第三集真相揭开，父女联手对抗开发商。",
        future_direction="父亲债务的真正债主浮出水面；母亲回城，三人关系重组。",
        character_arcs={"ch_lin": "从独自硬扛到接受父亲的帮助", "ch_zhou": "从逃避到承担"},
        characters=[
            Character(
                character_id="ch_lin",
                name="林晓",
                role="主角",
                relationships={"ch_zhou": "父亲（未知）", "ch_mei": "店员兼好友", "ch_mom": "母亲"},
                goal="保住书店",
                motivation="书店是她独自撑起的唯一依靠",
                knowledge=["父亲十五年前失踪", "父亲手背有烫伤疤"],
            ),
            Character(
                character_id="ch_zhou",
                name="周叔（林建国）",
                role="主角的父亲，隐藏身份的房东",
                relationships={"ch_lin": "女儿"},
                goal="还清旧债而不让女儿知道",
                motivation="愧疚",
                knowledge=["自己是林晓的父亲", "店铺已被抵押给赵经理"],
            ),
            Character(
                character_id="ch_mei",
                name="阿梅",
                role="主角的好友与帮手",
                relationships={"ch_lin": "好友"},
                goal="帮林晓保住店",
                motivation="书店也是她的工作",
            ),
            Character(
                character_id="ch_zhao",
                name="赵经理",
                role="对手，开发商代表",
                goal="按期收回整栋楼",
                motivation="开发项目的业绩",
                knowledge=["周叔把店铺抵押给他"],
            ),
            Character(
                character_id="ch_mom",
                name="林母",
                role="仅电话出场的母亲",
                relationships={"ch_lin": "女儿"},
                goal="不让女儿被父亲再次伤害",
                motivation="当年被抛下",
            ),
        ],
        looks=[
            CharacterLook(look_id="lk_lin_apron", character_id="ch_lin", description="毛衣配围裙"),
            CharacterLook(
                look_id="lk_zhou_rain",
                character_id="ch_zhou",
                description="雨夜赶来时被淋湿",
                applies_to_scene_ids=["sc_01_02"],
            ),
            CharacterLook(look_id="lk_zhou_home", character_id="ch_zhou", description="居家旧毛衫"),
            CharacterLook(look_id="lk_mei_work", character_id="ch_mei", description="书店工作服"),
            CharacterLook(look_id="lk_zhao_suit", character_id="ch_zhao", description="深灰西装"),
        ],
        locations=[
            Location(
                location_id="loc_shop",
                name="林晓的旧书店",
                summary="林晓独自经营、即将被收走的旧书店",
            ),
            Location(
                location_id="loc_room",
                name="周叔的出租屋",
                summary="周叔独居的简陋出租屋，藏着他的过去",
            ),
        ],
        location_states=[
            LocationState(
                location_state_id="ls_shop_day", location_id="loc_shop", description="白天营业"
            ),
            LocationState(
                location_state_id="ls_shop_night", location_id="loc_shop", description="雨夜打烊"
            ),
            LocationState(
                location_state_id="ls_room_day", location_id="loc_room", description="白天"
            ),
            LocationState(
                location_state_id="ls_room_night", location_id="loc_room", description="夜晚台灯"
            ),
        ],
        props=[
            Prop(
                prop_id="prop_photo",
                name="旧全家福",
                story_function="照片上父亲手背的烫伤疤让林晓认出周叔",
                owner_character_id="ch_lin",
            )
        ],
        prop_states=[
            PropState(prop_state_id="ps_photo_intact", prop_id="prop_photo", description="完整"),
            PropState(
                prop_state_id="ps_photo_torn",
                prop_id="prop_photo",
                description="争吵中被撕成两半",
            ),
        ],
        beat_outlines=[
            BeatOutline(
                episode_id="ep_01",
                opening="开发商限期搬走",
                protagonist_action="林晓等房东当面对质",
                cost_or_reversal="房东手上的疤与父亲一致",
                ending_change="林晓怀疑房东就是父亲",
                cliffhanger_setup_refs=["prop_photo"],
            ),
            BeatOutline(
                episode_id="ep_02",
                opening="林晓上门认亲",
                protagonist_action="林晓挡住拆店工人",
                cost_or_reversal="父亲否认，委托书出现",
                ending_change="委托书日期指向十五年前",
                cliffhanger_setup_refs=["ch_zhao"],
            ),
            BeatOutline(
                episode_id="ep_03",
                opening="阿梅发现抵押真相",
                protagonist_action="林晓撕掉合同",
                cost_or_reversal="父亲撕照片想推开女儿",
                ending_change="父女并肩，推土机已到门口",
                cliffhanger_setup_refs=["ch_zhao", "prop_photo"],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Script + storyboard are generated from one compact table so they stay consistent.

# (action, (speaker, line, voice_only) | None, prop_state_id | None)
BeatRow = tuple[str, tuple[str, str, bool] | None, str | None]


@dataclass
class SceneRow:
    scene_id: str
    int_ext: str
    location_id: str
    location_state_id: str
    time_of_day: str
    looks: dict[str, str]  # visible character_id -> look_id
    voice_only: list[str] = field(default_factory=list)
    beats: list[BeatRow] = field(default_factory=list)
    shot_reference_at: int | None = None  # index of a shot needing a composition reference


@dataclass
class EpisodeRow:
    episode_id: str
    title: str
    opening_event: str
    main_conflict: str
    ending_event: str
    scenes: list[SceneRow]


EPISODES: list[EpisodeRow] = [
    EpisodeRow(
        "ep_01",
        "最后一个月房租",
        "赵经理限期搬店",
        "林晓要见房东",
        "房东手上的疤",
        [
            SceneRow(
                "sc_01_01",
                "INT",
                "loc_shop",
                "ls_shop_day",
                "日",
                {"ch_lin": "lk_lin_apron", "ch_mei": "lk_mei_work", "ch_zhao": "lk_zhao_suit"},
                beats=[
                    (
                        "赵经理推门进店，把通知拍在柜台上。",
                        ("ch_zhao", "月底前搬走，房东已经签字了。", False),
                        None,
                    ),
                    (
                        "林晓攥紧通知，看向阿梅。",
                        ("ch_lin", "房东从没露过面，凭什么替我签？", False),
                        None,
                    ),
                    (
                        "阿梅翻出租约，指着签名。",
                        ("ch_mei", "租约上写着，房东今晚亲自来收最后一个月房租。", False),
                        None,
                    ),
                    ("林晓把旧全家福塞进围裙口袋，盯着门口。", None, "ps_photo_intact"),
                ],
            ),
            SceneRow(
                "sc_01_02",
                "INT",
                "loc_shop",
                "ls_shop_night",
                "夜",
                {"ch_lin": "lk_lin_apron", "ch_zhou": "lk_zhou_rain", "ch_mei": "lk_mei_work"},
                beats=[
                    (
                        "周叔推门进来，雨水顺着帽檐滴落。",
                        ("ch_zhou", "最后一个月，现金。", False),
                        None,
                    ),
                    (
                        "林晓递钱时，看见周叔手背上的烫伤疤。",
                        ("ch_lin", "这道疤……你是怎么烫的？", False),
                        None,
                    ),
                    ("周叔迅速缩回手，转身要走。", ("ch_zhou", "跟你没关系。", False), None),
                    (
                        "林晓举起全家福，照片上父亲的手背有同样的疤。",
                        ("ch_mei", "晓晓，照片上的人……", False),
                        "ps_photo_intact",
                    ),
                ],
                shot_reference_at=3,
            ),
        ],
    ),
    EpisodeRow(
        "ep_02",
        "他不认",
        "林晓上门认亲",
        "父亲否认，开发商逼拆",
        "委托书日期是十五年前",
        [
            SceneRow(
                "sc_02_01",
                "INT",
                "loc_room",
                "ls_room_day",
                "日",
                {"ch_lin": "lk_lin_apron", "ch_zhou": "lk_zhou_home"},
                voice_only=["ch_mom"],
                beats=[
                    (
                        "林晓敲开出租屋的门，举起全家福。",
                        ("ch_lin", "你就是林建国。", False),
                        "ps_photo_intact",
                    ),
                    (
                        "周叔看着照片沉默，墙上挂着同款旧外套。",
                        ("ch_zhou", "林建国十五年前就死了。", False),
                        "ps_photo_intact",
                    ),
                    (
                        "林晓手机响起，是母亲来电。",
                        ("ch_mom", "晓晓，你是不是去找他了？别信他。", True),
                        None,
                    ),
                    ("林晓挂断电话，周叔背过身。", None, None),
                ],
            ),
            SceneRow(
                "sc_02_02",
                "EXT",
                "loc_shop",
                "ls_shop_day",
                "日",
                {"ch_lin": "lk_lin_apron", "ch_zhao": "lk_zhao_suit"},
                beats=[
                    (
                        "赵经理带工人在店门口量尺寸。",
                        ("ch_zhao", "房东改主意了，明天就拆。", False),
                        None,
                    ),
                    ("林晓挡在门口。", ("ch_lin", "他是我爸，他不会这么做。", False), None),
                    (
                        "赵经理掏出一份签字的委托书。",
                        ("ch_zhao", "白纸黑字，你自己看。", False),
                        None,
                    ),
                    ("林晓看到签名旁的日期是十五年前。", None, None),
                ],
            ),
        ],
    ),
    EpisodeRow(
        "ep_03",
        "一起还",
        "阿梅带来抵押真相",
        "父亲想推开女儿",
        "推土机到门口",
        [
            SceneRow(
                "sc_03_01",
                "INT",
                "loc_room",
                "ls_room_night",
                "夜",
                {"ch_lin": "lk_lin_apron", "ch_zhou": "lk_zhou_home", "ch_mei": "lk_mei_work"},
                beats=[
                    (
                        "阿梅带林晓冲进出租屋，桌上摊着旧医院账单。",
                        ("ch_mei", "他把店抵押给赵经理，是为了还你妈当年的医药费。", False),
                        None,
                    ),
                    (
                        "周叔从林晓手中拿过全家福，撕成两半。",
                        ("ch_zhou", "就当我没回来过。", False),
                        "ps_photo_torn",
                    ),
                    (
                        "林晓把撕开的照片拼回桌上。",
                        ("ch_lin", "你欠我的，不是钱。", False),
                        "ps_photo_torn",
                    ),
                    ("周叔的手按在照片的裂缝上。", None, "ps_photo_torn"),
                ],
            ),
            SceneRow(
                "sc_03_02",
                "INT",
                "loc_shop",
                "ls_shop_day",
                "日",
                {"ch_lin": "lk_lin_apron", "ch_zhou": "lk_zhou_home", "ch_zhao": "lk_zhao_suit"},
                beats=[
                    (
                        "赵经理把合同推到柜台上。",
                        ("ch_zhao", "签了这份，你爸的债一笔勾销。", False),
                        None,
                    ),
                    ("周叔挡在林晓前面。", ("ch_zhou", "店是她的，债我来还。", False), None),
                    ("林晓撕掉合同。", ("ch_lin", "我们一起还。", False), None),
                    ("门外传来推土机声，三人同时看向门口。", None, None),
                ],
            ),
        ],
    ),
]


def _line_ms(text: str) -> int:
    return min(1000 + len(text) * MS_PER_CHAR, SHOT_MS - 2000)


def sample_episodes() -> list[Episode]:
    episodes: list[Episode] = []
    for e_no, er in enumerate(EPISODES, start=1):
        scenes = []
        for s_no, sr in enumerate(er.scenes, start=1):
            beats = []
            for b_no, (action, dlg, _prop) in enumerate(sr.beats, start=1):
                beat_id = f"bt_{sr.scene_id[3:]}_{b_no:02d}"
                line = None
                if dlg:
                    speaker, text, voice_only = dlg
                    line = DialogueLine(
                        dialogue_id=f"dl_{sr.scene_id[3:]}_{b_no:02d}",
                        character_id=speaker,
                        text=text,
                        voice_only=voice_only,
                    )
                beats.append(Beat(beat_id=beat_id, action=action, dialogue=line))
            scenes.append(
                Scene(
                    scene_id=sr.scene_id,
                    number=s_no,
                    int_ext=sr.int_ext,  # type: ignore[arg-type]
                    location_id=sr.location_id,
                    location_state_id=sr.location_state_id,
                    time_of_day=sr.time_of_day,
                    character_ids=list(sr.looks) + sr.voice_only,
                    target_duration_ms=SHOT_MS * len(sr.beats),
                    beats=beats,
                    end_state=SceneState(open_questions=[er.ending_event]),
                )
            )
        episodes.append(
            Episode(
                episode_id=er.episode_id,
                number=e_no,
                title=er.title,
                opening_event=er.opening_event,
                main_conflict=er.main_conflict,
                ending_event=er.ending_event,
                scenes=scenes,
            )
        )
    return episodes


def sample_storyboard() -> tuple[list[Shot], list[EpisodeTiming]]:
    shots: list[Shot] = []
    timings: list[EpisodeTiming] = []
    for er in EPISODES:
        cursor = 0
        schedule: list[DialogueScheduleEntry] = []
        for sr in er.scenes:
            for b_no, (action, dlg, prop_state) in enumerate(sr.beats, start=1):
                suffix = f"{sr.scene_id[3:]}_{b_no:02d}"
                cues = []
                if dlg:
                    start = cursor + 1000
                    end = start + _line_ms(dlg[1])
                    schedule.append(
                        DialogueScheduleEntry(
                            dialogue_id=f"dl_{suffix}", start_ms=start, end_ms=end
                        )
                    )
                    cues.append(DialogueCue(dialogue_id=f"dl_{suffix}", start_ms=start, end_ms=end))
                shots.append(
                    Shot(
                        shot_id=f"sh_{suffix}",
                        episode_id=er.episode_id,
                        scene_id=sr.scene_id,
                        start_ms=cursor,
                        duration_ms=SHOT_MS,
                        character_ids=list(sr.looks),
                        location_id=sr.location_id,
                        character_look_refs=[
                            CharacterLookRef(character_id=c, look_id=lk)
                            for c, lk in sr.looks.items()
                        ],
                        location_state_id=sr.location_state_id,
                        prop_state_refs=(
                            [PropStateRef(prop_id="prop_photo", prop_state_id=prop_state)]
                            if prop_state
                            else []
                        ),
                        framing="中景" if b_no % 2 else "近景",
                        camera="固定机位，平视",
                        visual_action=action,
                        dialogue_cues=cues,
                        sound="环境声",
                        continuity="服装与站位延续上一镜",
                        narrative_function=action,
                        source_beat_ids=[f"bt_{suffix}"],
                        needs_shot_reference=sr.shot_reference_at == b_no - 1,
                    )
                )
                cursor += SHOT_MS
        timings.append(
            EpisodeTiming(
                episode_id=er.episode_id,
                dialogue_schedule=schedule,
                speech_rate_assumption=f"zh-CN estimate {MS_PER_CHAR}ms/char, unmeasured",
            )
        )
    return shots, timings


def sample_cast_sheet() -> CastSheet:
    return CastSheet(
        characters=[
            CastMember(
                character_id="ch_lin",
                age_range="二十八岁左右",
                gender_presentation="女性",
                build_and_height="中等身高，偏瘦，肩背挺直",
                face="鹅蛋脸，眉眼清晰，习惯抿嘴，眼神倔强",
                hair="齐耳短发，深棕色，常别在耳后",
                default_wardrobe="深色粗针毛衣，外系帆布店铺围裙",
                wardrobe_palette="墨绿、深灰与米白",
                posture_and_mannerisms="说话时双手撑着柜台；紧张时整理书脊",
                voice="中音，语速偏快，北方口音",
                must_keep=["短发", "店铺围裙"],
                design_freedom=["毛衣的具体款式"],
            ),
            CastMember(
                character_id="ch_zhou",
                age_range="六十岁左右",
                gender_presentation="男性",
                build_and_height="偏高，略驼背，手掌粗大",
                face="面颊消瘦，皱纹深，目光躲闪",
                hair="灰白短发，略显凌乱",
                distinguishing_features="右手背一块旧烫伤疤，与全家福中父亲的疤一致",
                default_wardrobe="洗旧的棕色夹克，深色长裤",
                wardrobe_palette="褪色的棕与灰",
                posture_and_mannerisms="习惯把右手插进口袋遮住伤疤",
                voice="低沉沙哑，说话慢，常停顿",
                must_keep=["右手背烫伤疤的位置与形状", "旧夹克"],
                design_freedom=["皱纹与发型细节"],
            ),
            CastMember(
                character_id="ch_mei",
                age_range="二十六岁左右",
                gender_presentation="女性",
                build_and_height="娇小，动作利落",
                face="圆脸，笑容明显",
                hair="高马尾",
                default_wardrobe="书店统一的藏青工作服",
                wardrobe_palette="藏青与白",
                posture_and_mannerisms="走路快，总抱着一摞书",
                voice="清亮，语速快",
                must_keep=["马尾", "工作服颜色与林晓的围裙区分"],
            ),
            CastMember(
                character_id="ch_zhao",
                age_range="四十岁左右",
                gender_presentation="男性",
                build_and_height="中等身材，略发福",
                face="方脸，常带客套的笑",
                hair="梳得一丝不苟的背头",
                default_wardrobe="合身的深灰西装，白衬衫，无领带",
                wardrobe_palette="深灰与白，与书店的暖色对立",
                posture_and_mannerisms="站姿笔直，习惯看表",
                voice="中音，礼貌但不容商量",
                must_keep=["深灰西装"],
            ),
            CastMember(
                character_id="ch_mom",
                on_screen=False,
                age_range="五十多岁",
                gender_presentation="女性",
                build_and_height="不出镜",
                face="不出镜",
                hair="不出镜",
                default_wardrobe="不出镜",
                wardrobe_palette="不出镜",
                posture_and_mannerisms="不出镜",
                voice="温和但疲惫，南方口音，只在电话中出现",
            ),
        ],
        looks=[
            CastLook(
                look_id="lk_lin_apron",
                character_id="ch_lin",
                wardrobe="深色毛衣配帆布围裙",
                hair_and_makeup="素颜，短发别在耳后",
                changes_from_base="无，即基础造型",
            ),
            CastLook(
                look_id="lk_zhou_rain",
                character_id="ch_zhou",
                wardrobe="湿透的旧夹克，加一顶深色鸭舌帽",
                hair_and_makeup="头发贴在额头，脸上有雨水",
                condition="全身被雨淋湿",
                changes_from_base="淋湿，加鸭舌帽",
                must_keep=["鸭舌帽遮住半张脸"],
            ),
            CastLook(
                look_id="lk_zhou_home",
                character_id="ch_zhou",
                wardrobe="起球的灰色旧毛衫",
                hair_and_makeup="头发凌乱，未刮胡子",
                changes_from_base="脱下夹克，换居家毛衫",
            ),
            CastLook(
                look_id="lk_mei_work",
                character_id="ch_mei",
                wardrobe="书店藏青工作服",
                hair_and_makeup="高马尾，淡妆",
                changes_from_base="无，即基础造型",
            ),
            CastLook(
                look_id="lk_zhao_suit",
                character_id="ch_zhao",
                wardrobe="深灰西装",
                hair_and_makeup="背头，整洁",
                changes_from_base="无，即基础造型",
            ),
        ],
    )


def sample_set_sheet() -> SetSheet:
    return SetSheet(
        locations=[
            SetLocation(
                location_id="loc_shop",
                interior_exterior="INT",
                era_and_region="当代中国南方老城区",
                layout="狭长店面，入口玻璃门正对收银柜台，两侧高至天花板的书架",
                key_furnishings="木质收银柜台、旧书架、门口风铃、柜台上的台灯",
                materials_and_textures="磨旧的实木、泛黄纸张、斑驳的水磨石地面",
                color_palette="暖棕与米黄",
                lighting="白天自然光从门口进入；夜晚暖色吊灯与柜台台灯",
                entrances_and_blocking="唯一入口为临街玻璃门；人物从门口走向柜台，柜台后是林晓的固定位置",
                ambient_sound="门口风铃、街道车声",
                must_keep=["玻璃门正对柜台的布局", "门口风铃"],
                design_freedom=["书架上书籍的具体摆放"],
            ),
            SetLocation(
                location_id="loc_room",
                interior_exterior="INT",
                era_and_region="当代中国南方老旧居民楼",
                layout="单间，门对窗，左侧单人床，右侧小方桌",
                key_furnishings="方桌、单人床、墙上挂着的旧外套、桌上的饭盒",
                materials_and_textures="剥落的墙皮、旧木家具",
                color_palette="灰绿与暗黄",
                lighting="窗户为主光；夜晚只有一盏台灯",
                entrances_and_blocking="走廊一侧的木门；进门后正面是窗",
                ambient_sound="楼道脚步声、远处电视声",
                must_keep=["门对窗的布局"],
            ),
        ],
        states=[
            SetState(
                location_state_id="ls_shop_day",
                location_id="loc_shop",
                time_of_day="白天",
                lighting="门口进来的自然光",
                changes_from_base="营业中，门口挂着营业牌",
            ),
            SetState(
                location_state_id="ls_shop_night",
                location_id="loc_shop",
                time_of_day="雨夜",
                lighting="只开柜台台灯，玻璃门外有雨水反光",
                condition="窗外下雨",
                changes_from_base="打烊，卷帘半拉，门外下雨",
                must_keep=["玻璃门上的雨水"],
            ),
            SetState(
                location_state_id="ls_room_day",
                location_id="loc_room",
                time_of_day="白天",
                lighting="窗户的自然光",
                changes_from_base="无，即基础状态",
            ),
            SetState(
                location_state_id="ls_room_night",
                location_id="loc_room",
                time_of_day="夜晚",
                lighting="只有桌上台灯，房间大部分在暗处",
                changes_from_base="夜晚，仅台灯照明",
            ),
        ],
    )


def sample_prop_sheet() -> PropSheet:
    return PropSheet(
        props=[
            PropItem(
                prop_id="prop_photo",
                size_and_scale="五寸相片，装在巴掌大的旧相框里",
                materials="相纸，木质相框",
                color_and_finish="褪色的彩色照片，边缘泛黄",
                condition_and_wear="相框边角磨损",
                distinguishing_marks="照片上父亲的右手背可见烫伤疤",
                handling="林晓收在柜台抽屉里；近景时必须看清父亲的手",
                must_keep=["父亲右手背的烫伤疤清晰可见"],
                design_freedom=["一家三口的站位"],
            )
        ],
        states=[
            PropVariant(
                prop_state_id="ps_photo_intact",
                prop_id="prop_photo",
                condition="完整",
                changes_from_base="无，即基础状态",
            ),
            PropVariant(
                prop_state_id="ps_photo_torn",
                prop_id="prop_photo",
                condition="从中间撕成两半",
                changes_from_base="被撕开，裂口正好穿过父亲和林晓之间",
                must_keep=["裂口位置"],
            ),
        ],
    )


def sample_style_brief() -> StyleBrief:
    return StyleBrief(
        medium="真人写实",
        era_region="当代中国南方小城",
        color_and_light="暖黄室内光与冷色雨夜对比",
        aspect_ratio="9:16",
        assumptions=["纯文字种子，视觉风格为创作设定"],
    )


def sample_design_overrides() -> dict[str, AssetRequirement]:
    """Model-authored design text for a few requirements (structure is program-owned)."""
    shop_night = AssetRequirement(
        requirement_id="ar_location_loc_shop_ls_shop_night",
        asset_type="location",
        subject_refs=[
            {"entity_type": "location", "entity_id": "loc_shop", "variant_id": "ls_shop_night"}
        ],
        design_brief="雨夜打烊的旧书店。玻璃门正对柜台，门外雨幕可见。",
        must_keep=[
            "玻璃门到柜台之间无遮挡，周叔进门动作可被柜台后的林晓看见",
            "柜台、书架、门的相对位置与白天状态一致",
        ],
        design_freedom=["书脊颜色与吊灯样式可在统一风格内选择"],
        used_by=[{"episode_id": "ep_01", "scene_id": "sc_01_02", "shot_id": "sh_01_02_01"}],
    )
    return {shop_night.requirement_id: shop_night}
