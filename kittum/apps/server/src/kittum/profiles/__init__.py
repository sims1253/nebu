"""Protocol profiles for specific review communities, kept out of the domain-neutral core.

Entry points call `load_profiles()` once; it registers every built-in profile with
`kittum.protocol.register_profile` and its report targets with `kittum.report.register_target`,
and is safe to call again.
"""

from kittum.profiles.gba_amnog import GBA_AMNOG
from kittum.profiles.gba_amnog.reports import register_targets as register_gba_amnog_targets
from kittum.protocol import Profile, register_profile

BUILT_IN: tuple[Profile, ...] = (GBA_AMNOG,)


def load_profiles() -> None:
    for profile in BUILT_IN:
        register_profile(profile)
    register_gba_amnog_targets()


__all__ = ["BUILT_IN", "GBA_AMNOG", "load_profiles"]
