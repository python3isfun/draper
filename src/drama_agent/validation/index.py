"""ID lookup tables over a production package, shared by validators and revision logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ..models import ProductionPackage
from ..models.screenplay import DialogueLine, Episode, Scene
from ..models.story import Character, CharacterLook, Location, LocationState, Prop, PropState
from ..models.storyboard import Shot


@dataclass
class PackageIndex:
    characters: dict[str, Character] = field(default_factory=dict)
    looks: dict[str, CharacterLook] = field(default_factory=dict)
    locations: dict[str, Location] = field(default_factory=dict)
    location_states: dict[str, LocationState] = field(default_factory=dict)
    props: dict[str, Prop] = field(default_factory=dict)
    prop_states: dict[str, PropState] = field(default_factory=dict)
    episodes: dict[str, Episode] = field(default_factory=dict)
    scenes: dict[str, Scene] = field(default_factory=dict)
    scene_episode: dict[str, str] = field(default_factory=dict)  # scene_id -> episode_id
    beats: dict[str, str] = field(default_factory=dict)  # beat_id -> scene_id
    dialogue: dict[str, DialogueLine] = field(default_factory=dict)
    dialogue_episode: dict[str, str] = field(default_factory=dict)  # dialogue_id -> episode_id
    shots: dict[str, Shot] = field(default_factory=dict)
    duplicates: list[str] = field(default_factory=list)
    # Every registered ID, across all kinds, so a scene and a shot cannot share an ID.
    all_ids: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, pkg: ProductionPackage) -> PackageIndex:
        idx = cls()
        bible = pkg.story_bible

        def put(table: dict[str, Any] | None, key: str, value: Any = None) -> None:
            if key in idx.all_ids:
                idx.duplicates.append(key)
            idx.all_ids.add(key)
            if table is not None:
                table.setdefault(key, value)

        for c in bible.characters:
            put(idx.characters, c.character_id, c)
        for lk in bible.looks:
            put(idx.looks, lk.look_id, lk)
        for loc in bible.locations:
            put(idx.locations, loc.location_id, loc)
        for ls in bible.location_states:
            put(idx.location_states, ls.location_state_id, ls)
        for p in bible.props:
            put(idx.props, p.prop_id, p)
        for ps in bible.prop_states:
            put(idx.prop_states, ps.prop_state_id, ps)
        for ep in pkg.episodes:
            put(idx.episodes, ep.episode_id, ep)
            for sc in ep.scenes:
                put(idx.scenes, sc.scene_id, sc)
                idx.scene_episode[sc.scene_id] = ep.episode_id
                for b in sc.beats:
                    put(idx.beats, b.beat_id, sc.scene_id)
                    if b.dialogue:
                        put(idx.dialogue, b.dialogue.dialogue_id, b.dialogue)
                        idx.dialogue_episode[b.dialogue.dialogue_id] = ep.episode_id
        for sh in pkg.shot_list:
            put(idx.shots, sh.shot_id, sh)
        # Requirement IDs join the global namespace; within-table duplicates of requirements
        # are reported by check_asset_requirements.
        for rid in {r.requirement_id for r in pkg.asset_requirements.items}:
            put(None, rid)
        return idx


def lockable_objects(pkg: ProductionPackage) -> dict[str, BaseModel]:
    """Every object a user can lock or target, keyed by its stable ID."""
    idx = PackageIndex.build(pkg)
    objects: dict[str, BaseModel] = {}
    for table in (
        idx.characters,
        idx.looks,
        idx.locations,
        idx.location_states,
        idx.props,
        idx.prop_states,
        idx.episodes,
        idx.scenes,
        idx.dialogue,
        idx.shots,
    ):
        objects.update(table)
    objects.update(pkg.asset_requirements.by_id())
    return objects
