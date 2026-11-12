"""A complete, hard-check-passing sample drama: "The Last Month's Rent".

Used by FixtureModelClient so the whole pipeline and API run without a real model, and by
tests as a known-good baseline to mutate. The story is in American English and set in a
small Ohio river town today: Maya Collins is about to lose her used bookstore, and the
landlord who comes for the last month's rent is the father who left fifteen years ago.
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

SAMPLE_SEED = (
    "A young woman about to lose her bookstore discovers that the old man who comes to collect "
    "the rent is the father who disappeared years ago."
)
SHOT_MS = 15_000
MS_PER_WORD = 380  # Text-stage speech-rate estimate; not a measured voice track.


def sample_request() -> JobRequest:
    return JobRequest.model_validate(
        {
            "sources": [{"type": "text", "text": SAMPLE_SEED, "use_as": "story_seed"}],
            "options": {"language": "en-US"},
        }
    )


def sample_source_brief(request: JobRequest | None = None) -> SourceBrief:
    request = request or sample_request()
    return SourceBrief(
        language=request.options.language or "en-US",
        observed_facts=[
            "The protagonist is about to lose her shop",
            "The old man collecting the rent is her long-missing father",
        ],
        user_constraints=["Three episodes, 120 seconds each, 2-3 scenes and 3-5 characters each"],
        creative_assumptions=[
            "The protagonist is Maya Collins, who runs a used bookstore in a small Ohio river town",
            "Her father left to pay off her mother's old hospital bills",
            "Derek Ross, a developer's agent, is the outside pressure",
        ],
        source_coverage=[
            SourceCoverage(
                source_index=i, analyzed_sections=["full text"], notes="single sentence seed"
            )
            for i in range(len(request.sources))
        ],
        creative_brief=CreativeBrief(
            genre="Family drama",
            audience_assumption="US short-drama viewers aged 25-45",
            core_conflict="Saving the store vs. facing the father who walked out",
            emotional_experience="Suspicion, then anger, then the pull toward forgiveness",
            reusable_assets=["the bookstore", "the father's rented room", "the old family photo"],
        ),
    )


def sample_story_bible() -> StoryBible:
    return StoryBible(
        title="The Last Month's Rent",
        logline=(
            "About to lose her used bookstore, Maya Collins discovers that the landlord collecting "
            "the last month's rent is the father who vanished fifteen years ago."
        ),
        genre="Family drama",
        audience_assumption="US short-drama viewers aged 25-45",
        emotional_goal="From suspicion to standing side by side",
        core_conflict="The developer's eviction deadline vs. fifteen years of silence",
        series_synopsis=(
            "Episode 1: Maya recognizes the scar. Episode 2: her father denies it and a signed "
            "authorization surfaces. Episode 3: the truth comes out and father and daughter face "
            "the developer together."
        ),
        future_direction=(
            "The real holder of the father's debt surfaces; Maya's mother comes back to town "
            "and the three of them have to rebuild."
        ),
        character_arcs={
            "ch_maya": "From carrying everything alone to accepting her father's help",
            "ch_frank": "From hiding to taking responsibility",
        },
        characters=[
            Character(
                character_id="ch_maya",
                name="Maya Collins",
                role="Protagonist",
                relationships={
                    "ch_frank": "Father (she doesn't know at first)",
                    "ch_jess": "Employee and best friend",
                    "ch_mom": "Mother",
                },
                goal="Keep the bookstore",
                motivation="The store is the one thing she built on her own",
                knowledge=[
                    "Her father disappeared fifteen years ago",
                    "Her father had a burn scar on the back of his hand",
                ],
            ),
            Character(
                character_id="ch_frank",
                name="Frank Collins",
                role="Maya's father, posing as the landlord Frank Hale",
                relationships={"ch_maya": "Daughter"},
                goal="Pay off the old debt without his daughter finding out",
                motivation="Guilt",
                knowledge=[
                    "He is Maya's father",
                    "The store's building is mortgaged to Derek Ross's company",
                ],
            ),
            Character(
                character_id="ch_jess",
                name="Jess Parker",
                role="Maya's best friend and right hand",
                relationships={"ch_maya": "Best friend"},
                goal="Help Maya keep the store",
                motivation="The store is her job too",
            ),
            Character(
                character_id="ch_ross",
                name="Derek Ross",
                role="Antagonist, the developer's agent",
                goal="Clear the whole building on schedule",
                motivation="His numbers on the development project",
                knowledge=["Frank mortgaged the building to his company"],
            ),
            Character(
                character_id="ch_mom",
                name="Linda Collins",
                role="Maya's mother, heard only on the phone",
                relationships={"ch_maya": "Daughter"},
                goal="Keep her daughter from being hurt by Frank again",
                motivation="She was the one he left behind",
            ),
        ],
        looks=[
            CharacterLook(
                look_id="lk_maya_apron",
                character_id="ch_maya",
                description="Sweater and shop apron",
            ),
            CharacterLook(
                look_id="lk_frank_rain",
                character_id="ch_frank",
                description="Soaked from walking over on a rainy night",
                applies_to_scene_ids=["sc_01_02"],
            ),
            CharacterLook(
                look_id="lk_frank_home",
                character_id="ch_frank",
                description="Worn cardigan at home",
            ),
            CharacterLook(
                look_id="lk_jess_work", character_id="ch_jess", description="Bookstore work shirt"
            ),
            CharacterLook(
                look_id="lk_ross_suit", character_id="ch_ross", description="Charcoal suit"
            ),
        ],
        locations=[
            Location(
                location_id="loc_shop",
                name="Collins Used Books",
                summary="Maya's used bookstore on Main Street, about to be taken from her",
            ),
            Location(
                location_id="loc_room",
                name="Frank's rented room",
                summary="The bare room Frank rents above a hardware store, full of his past",
            ),
        ],
        location_states=[
            LocationState(
                location_state_id="ls_shop_day",
                location_id="loc_shop",
                description="Open for business",
            ),
            LocationState(
                location_state_id="ls_shop_night",
                location_id="loc_shop",
                description="Closing up on a rainy night",
            ),
            LocationState(
                location_state_id="ls_room_day", location_id="loc_room", description="Daytime"
            ),
            LocationState(
                location_state_id="ls_room_night",
                location_id="loc_room",
                description="Night, lit by one desk lamp",
            ),
        ],
        props=[
            Prop(
                prop_id="prop_photo",
                name="the old family photo",
                story_function="The burn scar on her father's hand in the photo lets Maya recognize Frank",
                owner_character_id="ch_maya",
            )
        ],
        prop_states=[
            PropState(prop_state_id="ps_photo_intact", prop_id="prop_photo", description="Intact"),
            PropState(
                prop_state_id="ps_photo_torn",
                prop_id="prop_photo",
                description="Torn in half during the argument",
            ),
        ],
        beat_outlines=[
            BeatOutline(
                episode_id="ep_01",
                opening="The developer gives Maya a deadline to move out",
                protagonist_action="Maya waits to confront the landlord in person",
                cost_or_reversal="The landlord's scar matches her father's",
                ending_change="Maya suspects the landlord is her father",
                cliffhanger_setup_refs=["prop_photo"],
            ),
            BeatOutline(
                episode_id="ep_02",
                opening="Maya confronts Frank at his rented room",
                protagonist_action="Maya blocks the demolition crew",
                cost_or_reversal="Frank denies it, and a signed authorization appears",
                ending_change="The authorization is dated fifteen years ago",
                cliffhanger_setup_refs=["ch_ross"],
            ),
            BeatOutline(
                episode_id="ep_03",
                opening="Jess uncovers the mortgage",
                protagonist_action="Maya tears up the contract",
                cost_or_reversal="Frank tears the photo to push his daughter away",
                ending_change="Father and daughter stand together as the excavator arrives",
                cliffhanger_setup_refs=["ch_ross", "prop_photo"],
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


MAYA = {"ch_maya": "lk_maya_apron"}

EPISODES: list[EpisodeRow] = [
    EpisodeRow(
        "ep_01",
        "The Last Month's Rent",
        "Derek Ross gives Maya a deadline to move out",
        "Maya insists on meeting the landlord",
        "The scar on the landlord's hand",
        [
            SceneRow(
                "sc_01_01",
                "INT",
                "loc_shop",
                "ls_shop_day",
                "DAY",
                {**MAYA, "ch_jess": "lk_jess_work", "ch_ross": "lk_ross_suit"},
                beats=[
                    (
                        "Derek Ross pushes through the door and slaps a notice on the counter.",
                        (
                            "ch_ross",
                            "You're out by the end of the month. The landlord signed.",
                            False,
                        ),
                        None,
                    ),
                    (
                        "Maya crumples the notice and looks at Jess.",
                        (
                            "ch_maya",
                            "The landlord's never shown his face. How can he sign for me?",
                            False,
                        ),
                        None,
                    ),
                    (
                        "Jess pulls out the lease and points at the signature.",
                        (
                            "ch_jess",
                            "It says he's coming tonight to collect the last month's rent himself.",
                            False,
                        ),
                        None,
                    ),
                    (
                        "Maya slips the old family photo into her apron pocket and watches the door.",
                        None,
                        "ps_photo_intact",
                    ),
                ],
            ),
            SceneRow(
                "sc_01_02",
                "INT",
                "loc_shop",
                "ls_shop_night",
                "NIGHT",
                {**MAYA, "ch_frank": "lk_frank_rain", "ch_jess": "lk_jess_work"},
                beats=[
                    (
                        "Frank pushes the door open, rain dripping from the brim of his cap.",
                        ("ch_frank", "Last month. Cash.", False),
                        None,
                    ),
                    (
                        "As Maya hands over the money, she sees the burn scar on the back of his hand.",
                        ("ch_maya", "That scar... how did you get it?", False),
                        None,
                    ),
                    (
                        "Frank pulls his hand back and turns to leave.",
                        ("ch_frank", "None of your business.", False),
                        None,
                    ),
                    (
                        "Maya holds up the family photo; the father in it has the same scar.",
                        ("ch_jess", "Maya, the man in that picture...", False),
                        "ps_photo_intact",
                    ),
                ],
                shot_reference_at=3,
            ),
        ],
    ),
    EpisodeRow(
        "ep_02",
        "He Won't Admit It",
        "Maya confronts Frank at his room",
        "Frank denies it while the developer pushes to demolish",
        "The authorization is dated fifteen years ago",
        [
            SceneRow(
                "sc_02_01",
                "INT",
                "loc_room",
                "ls_room_day",
                "DAY",
                {**MAYA, "ch_frank": "lk_frank_home"},
                voice_only=["ch_mom"],
                beats=[
                    (
                        "Maya knocks on the door of the rented room and holds up the family photo.",
                        ("ch_maya", "You're Frank Collins.", False),
                        "ps_photo_intact",
                    ),
                    (
                        "Frank stares at the photo in silence; the same old jacket hangs on the wall.",
                        ("ch_frank", "Frank Collins died fifteen years ago.", False),
                        "ps_photo_intact",
                    ),
                    (
                        "Maya's phone rings. It's her mother.",
                        (
                            "ch_mom",
                            "Maya, did you go looking for him? Don't believe a word he says.",
                            True,
                        ),
                        None,
                    ),
                    ("Maya hangs up. Frank turns his back to her.", None, None),
                ],
            ),
            SceneRow(
                "sc_02_02",
                "EXT",
                "loc_shop",
                "ls_shop_day",
                "DAY",
                {**MAYA, "ch_ross": "lk_ross_suit"},
                beats=[
                    (
                        "Derek Ross has a crew measuring the storefront.",
                        (
                            "ch_ross",
                            "The landlord changed his mind. We start demolition tomorrow.",
                            False,
                        ),
                        None,
                    ),
                    (
                        "Maya plants herself in the doorway.",
                        ("ch_maya", "He's my father. He wouldn't do this.", False),
                        None,
                    ),
                    (
                        "Ross pulls out a signed authorization.",
                        ("ch_ross", "It's all in writing. See for yourself.", False),
                        None,
                    ),
                    ("Maya sees the date next to the signature: fifteen years ago.", None, None),
                ],
            ),
        ],
    ),
    EpisodeRow(
        "ep_03",
        "We Pay It Together",
        "Jess brings the truth about the mortgage",
        "Frank tries to push his daughter away",
        "The excavator arrives at the door",
        [
            SceneRow(
                "sc_03_01",
                "INT",
                "loc_room",
                "ls_room_night",
                "NIGHT",
                {**MAYA, "ch_frank": "lk_frank_home", "ch_jess": "lk_jess_work"},
                beats=[
                    (
                        "Jess and Maya burst into the room; old hospital bills cover the table.",
                        (
                            "ch_jess",
                            "He mortgaged the building to pay your mom's hospital bills.",
                            False,
                        ),
                        None,
                    ),
                    (
                        "Frank takes the family photo from Maya and tears it in half.",
                        ("ch_frank", "Pretend I never came back.", False),
                        "ps_photo_torn",
                    ),
                    (
                        "Maya fits the torn halves back together on the table.",
                        ("ch_maya", "What you owe me isn't money.", False),
                        "ps_photo_torn",
                    ),
                    ("Frank presses his hand over the tear in the photo.", None, "ps_photo_torn"),
                ],
            ),
            SceneRow(
                "sc_03_02",
                "INT",
                "loc_shop",
                "ls_shop_day",
                "DAY",
                {**MAYA, "ch_frank": "lk_frank_home", "ch_ross": "lk_ross_suit"},
                beats=[
                    (
                        "Derek Ross slides a contract across the counter.",
                        ("ch_ross", "Sign this and your dad's debt goes away.", False),
                        None,
                    ),
                    (
                        "Frank steps in front of Maya.",
                        ("ch_frank", "The store is hers. The debt is mine.", False),
                        None,
                    ),
                    (
                        "Maya tears up the contract.",
                        ("ch_maya", "We pay it together.", False),
                        None,
                    ),
                    (
                        "An excavator rumbles outside; all three turn toward the door.",
                        None,
                        None,
                    ),
                ],
            ),
        ],
    ),
]


def _line_ms(text: str) -> int:
    return min(1000 + len(text.split()) * MS_PER_WORD, SHOT_MS - 2000)


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
                        framing="Medium shot" if b_no % 2 else "Close-up",
                        camera="Static camera at eye level",
                        visual_action=action,
                        dialogue_cues=cues,
                        sound="Room tone",
                        continuity="Wardrobe and positions continue from the previous shot",
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
                speech_rate_assumption=f"en-US estimate {MS_PER_WORD}ms/word, unmeasured",
            )
        )
    return shots, timings


def sample_cast_sheet() -> CastSheet:
    return CastSheet(
        characters=[
            CastMember(
                character_id="ch_maya",
                age_range="About 28",
                gender_presentation="Woman",
                build_and_height="Average height, slim, straight-backed",
                face="Oval face, clear brows, a stubborn set to her mouth",
                hair="Chin-length dark brown hair, usually tucked behind her ears",
                default_wardrobe="Chunky dark knit sweater under a canvas shop apron",
                wardrobe_palette="Forest green, charcoal and off-white",
                posture_and_mannerisms="Leans on the counter with both hands; straightens book spines when nervous",
                voice="Mid-range, quick, plain Midwestern accent",
                must_keep=["Chin-length hair", "The shop apron"],
                design_freedom=["The sweater's exact pattern"],
            ),
            CastMember(
                character_id="ch_frank",
                age_range="About 60",
                gender_presentation="Man",
                build_and_height="Tall with a slight stoop, big hands",
                face="Hollow cheeks, deep lines, eyes that avoid yours",
                hair="Short gray hair, a little unkempt",
                distinguishing_features=(
                    "An old burn scar on the back of his right hand, matching the father's scar "
                    "in the family photo"
                ),
                default_wardrobe="A faded brown canvas work jacket and dark trousers",
                wardrobe_palette="Faded browns and grays",
                posture_and_mannerisms="Keeps his right hand in his pocket to hide the scar",
                voice="Low and rough, slow, with long pauses",
                must_keep=[
                    "The position and shape of the scar on his right hand",
                    "The old work jacket",
                ],
                design_freedom=["Wrinkles and hairstyle details"],
            ),
            CastMember(
                character_id="ch_jess",
                age_range="About 26",
                gender_presentation="Woman",
                build_and_height="Petite and quick-moving",
                face="Round face, an easy smile",
                hair="High ponytail",
                default_wardrobe="The store's navy work shirt",
                wardrobe_palette="Navy and white",
                posture_and_mannerisms="Walks fast, always carrying a stack of books",
                voice="Bright and fast",
                must_keep=["Ponytail", "A work shirt color distinct from Maya's apron"],
            ),
            CastMember(
                character_id="ch_ross",
                age_range="About 40",
                gender_presentation="Man",
                build_and_height="Medium build, a little heavy",
                face="Square face with a practiced, polite smile",
                hair="Neatly combed back",
                default_wardrobe="A fitted charcoal suit, white shirt, no tie",
                wardrobe_palette="Charcoal and white, against the store's warm colors",
                posture_and_mannerisms="Stands very straight; checks his watch",
                voice="Mid-range, polite but final",
                must_keep=["The charcoal suit"],
            ),
            CastMember(
                character_id="ch_mom",
                on_screen=False,
                age_range="Mid-fifties",
                gender_presentation="Woman",
                build_and_height="Not seen",
                face="Not seen",
                hair="Not seen",
                default_wardrobe="Not seen",
                wardrobe_palette="Not seen",
                posture_and_mannerisms="Not seen",
                voice="Warm but tired, heard only on the phone",
            ),
        ],
        looks=[
            CastLook(
                look_id="lk_maya_apron",
                character_id="ch_maya",
                wardrobe="Dark knit sweater and canvas apron",
                hair_and_makeup="No makeup, hair tucked behind her ears",
                changes_from_base="None; this is the base look",
            ),
            CastLook(
                look_id="lk_frank_rain",
                character_id="ch_frank",
                wardrobe="The old work jacket, soaked through, and a dark flat cap",
                hair_and_makeup="Hair plastered to his forehead, rain on his face",
                condition="Soaked by rain",
                changes_from_base="Wet through, with a flat cap",
                must_keep=["The cap brim shading half his face"],
            ),
            CastLook(
                look_id="lk_frank_home",
                character_id="ch_frank",
                wardrobe="A pilled gray cardigan",
                hair_and_makeup="Unkempt hair, unshaven",
                changes_from_base="Jacket off, cardigan on",
            ),
            CastLook(
                look_id="lk_jess_work",
                character_id="ch_jess",
                wardrobe="The store's navy work shirt",
                hair_and_makeup="High ponytail, light makeup",
                changes_from_base="None; this is the base look",
            ),
            CastLook(
                look_id="lk_ross_suit",
                character_id="ch_ross",
                wardrobe="Charcoal suit",
                hair_and_makeup="Hair combed back, clean-shaven",
                changes_from_base="None; this is the base look",
            ),
        ],
    )


def sample_set_sheet() -> SetSheet:
    return SetSheet(
        locations=[
            SetLocation(
                location_id="loc_shop",
                interior_exterior="INT",
                era_and_region="Present-day small river town in Ohio, an old Main Street storefront",
                layout=(
                    "A long, narrow store; the glass front door faces the checkout counter at the "
                    "back, with floor-to-ceiling bookshelves down both sides"
                ),
                key_furnishings="Wooden checkout counter, old bookshelves, a bell over the door, a desk lamp on the counter",
                materials_and_textures="Worn hardwood, yellowed paper, scuffed hardwood floor",
                color_palette="Warm browns and cream",
                lighting="Daylight through the front door by day; warm pendant lights and the counter lamp at night",
                entrances_and_blocking=(
                    "The glass front door is the only entrance; people walk from the door to the "
                    "counter, and Maya's place is behind the counter"
                ),
                ambient_sound="The door bell, traffic on Main Street",
                must_keep=["The front door facing the counter", "The bell over the door"],
                design_freedom=["How the books are arranged on the shelves"],
            ),
            SetLocation(
                location_id="loc_room",
                interior_exterior="INT",
                era_and_region="Present-day small Ohio town, a room above a hardware store",
                layout="One room; the door faces the window, a single bed on the left, a small table on the right",
                key_furnishings="Small table, single bed, an old jacket on a wall hook, a takeout container on the table",
                materials_and_textures="Peeling paint, old wooden furniture",
                color_palette="Gray-green and dull yellow",
                lighting="The window is the main light; at night only a desk lamp",
                entrances_and_blocking="A wooden door from the hallway; the window is straight ahead as you enter",
                ambient_sound="Footsteps on the stairs, a TV through the wall",
                must_keep=["The door facing the window"],
            ),
        ],
        states=[
            SetState(
                location_state_id="ls_shop_day",
                location_id="loc_shop",
                time_of_day="Day",
                lighting="Daylight through the front door",
                changes_from_base='Open for business, with an "OPEN" sign hanging on the door',
            ),
            SetState(
                location_state_id="ls_shop_night",
                location_id="loc_shop",
                time_of_day="Rainy night",
                lighting="Only the counter lamp on; rain reflections on the glass door",
                condition="Raining outside",
                changes_from_base="Closing up, the blind half down, rain outside",
                must_keep=["Rain on the glass door"],
            ),
            SetState(
                location_state_id="ls_room_day",
                location_id="loc_room",
                time_of_day="Day",
                lighting="Daylight from the window",
                changes_from_base="None; this is the base state",
            ),
            SetState(
                location_state_id="ls_room_night",
                location_id="loc_room",
                time_of_day="Night",
                lighting="Only the desk lamp; most of the room in shadow",
                changes_from_base="Night, lit only by the desk lamp",
            ),
        ],
    )


def sample_prop_sheet() -> PropSheet:
    return PropSheet(
        props=[
            PropItem(
                prop_id="prop_photo",
                size_and_scale="A 4x6 print in a palm-sized old frame",
                materials="Photo paper, wooden frame",
                color_and_finish="A faded color print with yellowed edges",
                condition_and_wear="Worn frame corners",
                distinguishing_marks="The burn scar on the back of the father's right hand is visible in the photo",
                handling="Maya keeps it in the counter drawer; close-ups must show the father's hand clearly",
                must_keep=["The burn scar on the father's right hand, clearly visible"],
                design_freedom=["How the family of three is posed"],
            )
        ],
        states=[
            PropVariant(
                prop_state_id="ps_photo_intact",
                prop_id="prop_photo",
                condition="Intact",
                changes_from_base="None; this is the base state",
            ),
            PropVariant(
                prop_state_id="ps_photo_torn",
                prop_id="prop_photo",
                condition="Torn in half down the middle",
                changes_from_base="Torn, the tear running exactly between the father and Maya",
                must_keep=["Where the tear runs"],
            ),
        ],
    )


def sample_style_brief() -> StyleBrief:
    return StyleBrief(
        medium="Live-action, naturalistic",
        era_region="Present-day small-town Ohio",
        color_and_light="Warm interior light against cool rainy nights",
        aspect_ratio="9:16",
        assumptions=["Text-only seed; the visual style is a creative choice"],
    )


def sample_design_overrides() -> dict[str, AssetRequirement]:
    """Model-authored design text for a few requirements (structure is program-owned)."""
    shop_night = AssetRequirement(
        requirement_id="ar_location_loc_shop_ls_shop_night",
        asset_type="location",
        subject_refs=[
            {"entity_type": "location", "entity_id": "loc_shop", "variant_id": "ls_shop_night"}
        ],
        design_brief=(
            "The used bookstore closing up on a rainy night. The glass front door faces the "
            "counter, with rain visible outside."
        ),
        must_keep=[
            "A clear line from the front door to the counter, so Maya behind the counter sees Frank come in",
            "Counter, shelves and door in the same positions as in the daytime state",
        ],
        design_freedom=["Book spine colors and pendant lamp style, within one consistent look"],
        used_by=[{"episode_id": "ep_01", "scene_id": "sc_01_02", "shot_id": "sh_01_02_01"}],
    )
    return {shop_night.requirement_id: shop_night}
