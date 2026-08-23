from __future__ import annotations

import unittest
from dataclasses import dataclass

from server.errors import ApiError
from server.prompting import compile_prompt


@dataclass(frozen=True)
class Reference:
    kind: str
    role: str = "reference"
    aliases: tuple[str, ...] = ()
    include_audio: bool = False
    voice_speaker: str = "S1"
    voice_subject: int = 1


class ReferencePromptTests(unittest.TestCase):
    def test_modal_labels_remain_distinct_and_follow_native_presentation_order(self) -> None:
        references = (
            Reference("audio", "voice", ("voice.wav",)),
            Reference("image", "identity", ("hero.png",)),
            Reference("video", "camera", ("camera.mp4",), include_audio=True),
            Reference("image", "composition", ("board.png",)),
        )
        result = compile_prompt(
            "A performer enters the room.",
            mode="ref2va",
            references=references,
            parts={"sound": "Soft footsteps and room tone.", "music": "no music"},
        )
        definitions = result.split("\n\nsummary:", 1)[0]
        self.assertIn("<Subject 1> is the reusable visible identity shown in <Picture 1>", definitions)
        self.assertIn("<Picture 2> is a composition anchor", definitions)
        self.assertIn("<Video 1> is the camera-movement structure reference", definitions)
        self.assertIn("<Audio 1> is the enabled synchronized audio track of <Video 1>", definitions)
        self.assertIn("<Audio 2> is the voice timbre and delivery reference", definitions)
        self.assertNotIn("<Subject 2>", definitions)
        self.assertIn("[reference generation + audio reference]", result)

    def test_video_without_enabled_soundtrack_has_no_audio_label(self) -> None:
        result = compile_prompt(
            "Follow the camera path.",
            mode="ref2va",
            references=(Reference("video", "camera", ("clip",)),),
        )
        self.assertIn("<Video 1>", result)
        self.assertNotIn("<Audio 1>", result)

    def test_motion_is_visible_subject_but_source_video_is_not_renamed(self) -> None:
        result = compile_prompt(
            "Transfer the gesture.",
            mode="ref2va",
            references=(Reference("video", "motion", ("gesture",)),),
        )
        self.assertIn("<Subject 1> is the reusable visible motion pattern demonstrated in <Video 1>", result)
        self.assertIn("attribute_transfer", result)


class SoundPromptTests(unittest.TestCase):
    def test_diegetic_sound_and_no_score_are_independent(self) -> None:
        result = compile_prompt(
            "A cyclist crosses the wet street.",
            mode="text",
            parts={"sound": "Rain, tire spray, and a bicycle chain are audible.", "music": "不要配乐"},
        )
        self.assertIn("overall_soundscape: Rain, tire spray, and a bicycle chain are audible.", result)
        self.assertIn("non_diegetic_music: N/A", result)

    def test_dialogue_is_not_repeated_in_overall_soundscape(self) -> None:
        result = compile_prompt(
            "A woman turns toward the door.",
            mode="text",
            parts={"dialogue": "<d>[Chinese] 我们出发。</d>", "sound": "A door latch clicks."},
        )
        detailed, soundscape = result.split("\n\noverall_soundscape:", 1)
        self.assertIn("我们出发", detailed)
        self.assertNotIn("我们出发", soundscape)
        self.assertIn("(S1) says: <d>[Chinese]", detailed)

    def test_dialogue_requires_language_markup_and_preserves_stage_direction(self) -> None:
        result = compile_prompt(
            "Two performers stand on opposite sides of a door.",
            mode="text",
            parts={"dialogue": "[offscreen] (S2) <d>[Chinese] 别开门。</d>"},
        )
        self.assertIn("(S2) says in an off-screen voiceover", result)
        self.assertIn("every visible character keeps their lips closed", result)
        self.assertNotIn("[offscreen]", result)
        with self.assertRaisesRegex(ApiError, "each dialogue line"):
            compile_prompt("A close-up.", mode="text", parts={"dialogue": "别开门"})

    def test_voice_reference_is_bound_to_declared_speaker(self) -> None:
        result = compile_prompt(
            "A presenter addresses camera.", mode="ref2va",
            references=(Reference("image", "identity"), Reference("audio", "voice", voice_speaker="S3", voice_subject=1)),
            parts={"dialogue": "(S3) <d>[English] Welcome back.</d>"},
        )
        self.assertIn("<Audio 1> is the voice timbre and delivery reference for <Subject 1> (S3)", result)

    def test_cross_cut_cutoff_and_simultaneous_speakers_compile_to_h3_semantics(self) -> None:
        result = compile_prompt(
            "The camera cuts between two rooms.", mode="text",
            parts={"dialogue": "[cross-cut] [cutoff] (S1,S2) <d>[English] Stop right—</d>"},
        )
        self.assertIn("(S1,S2) say simultaneously", result)
        self.assertIn("<scenetrans> The same voice continues seamlessly", result)
        self.assertIn("<cutoff>", result)

    def test_voice_binding_rejects_unknown_or_duplicate_targets(self) -> None:
        with self.assertRaisesRegex(ApiError, "no matching structured dialogue"):
            compile_prompt(
                "A presenter speaks.", mode="ref2va",
                references=(Reference("image", "identity"), Reference("audio", "voice", voice_speaker="S2")),
                parts={"dialogue": "(S1) <d>[English] Hello.</d>"},
            )
        with self.assertRaisesRegex(ApiError, "only one voice reference"):
            compile_prompt(
                "A presenter speaks.", mode="ref2va",
                references=(Reference("image", "identity"), Reference("audio", "voice"), Reference("audio", "voice")),
                parts={"dialogue": "(S1) <d>[English] Hello.</d>"},
            )

    def test_ref_summary_does_not_repeat_structured_dialogue(self) -> None:
        result = compile_prompt(
            "A woman turns toward the door.",
            mode="ref2va",
            references=(Reference("image", "identity"),),
            parts={"dialogue": "<d>[Chinese] 我们出发。</d>"},
        )
        summary = result.split("summary: ", 1)[1].split("\n\nretention_analysis:", 1)[0]
        detailed = result.split("detailed_description: ", 1)[1].split("\n\noverall_soundscape:", 1)[0]
        self.assertNotIn("我们出发", summary)
        self.assertIn("我们出发", detailed)

    def test_complete_silence_rejects_music_or_enabled_audio_reference(self) -> None:
        with self.assertRaisesRegex(ApiError, "complete silence"):
            compile_prompt("An empty room.", mode="text", parts={"sound": "complete silence", "music": "slow piano"})
        with self.assertRaisesRegex(ApiError, "complete silence"):
            compile_prompt(
                "An empty room.",
                mode="ref2va",
                references=(Reference("video", "camera", include_audio=True),),
                parts={"sound": "complete silence", "music": "N/A"},
            )

    def test_music_reference_and_explicit_no_score_cannot_conflict(self) -> None:
        music = (Reference("audio", "music"),)
        result = compile_prompt("A title sequence.", mode="ref2va", references=music)
        music_section = result.split("non_diegetic_music: ", 1)[1]
        self.assertIn("<Audio 1>", music_section)
        self.assertNotEqual(music_section, "N/A")
        with self.assertRaisesRegex(ApiError, "music reference"):
            compile_prompt(
                "A title sequence.",
                mode="ref2va",
                references=music,
                parts={"music": "no music"},
            )

    def test_voice_reference_is_not_misclassified_as_ambient_sound(self) -> None:
        result = compile_prompt(
            "A narrator introduces the scene.",
            mode="ref2va",
            references=(Reference("audio", "voice"), Reference("image", "identity")),
        )
        soundscape = result.split("overall_soundscape: ", 1)[1].split("\n\nnon_diegetic_music:", 1)[0]
        self.assertNotIn("<Audio 1>", soundscape)


class KeyframePromptTests(unittest.TestCase):
    def test_i2va_explicitly_preserves_first_frame_visual_relations(self) -> None:
        result = compile_prompt(
            "The dancer turns toward camera.", mode="fl2va",
            references=(Reference("image", "first_frame"),), duration_actual=124 / 24,
        )
        self.assertIn("visible subject identity, clothing, colors, key objects, composition, and spatial relationships", result)

    def test_fl2va_uses_effective_duration_and_actual_final_shot_once(self) -> None:
        result = compile_prompt(
            "[Shot 1] She closes the umbrella. [Shot 2] At 00:03.000, the camera cuts to the doorway.",
            mode="fl2va",
            references=(Reference("image", "first_frame"), Reference("image", "last_frame")),
            duration_actual=124 / 24,
            parts={"music": "N/A"},
        )
        self.assertTrue(result.startswith(
            "How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
            "Picture 2 (from Shot 2) aligns with the 5.17-second mark of the target video."
        ))
        body = result.split("integrated_multimodal_description: ", 1)[1].split("\n\noverall_soundscape", 1)[0]
        self.assertEqual(body.count("[Shot 1]"), 1)
        self.assertEqual(body.count("[Shot 2]"), 1)
        self.assertIn("ends on <Picture 2>", body)

    def test_l2va_requires_duration_and_uses_last_shot(self) -> None:
        reference = (Reference("image", "last_frame"),)
        with self.assertRaisesRegex(ApiError, "effective duration"):
            compile_prompt("A figure approaches.", mode="fl2va", references=reference)
        result = compile_prompt(
            "[Shot 1] A figure approaches. [Shot 2] At 00:04.000, the figure stops.",
            mode="fl2va",
            references=reference,
            duration_actual=6.25,
        )
        self.assertTrue(result.startswith(
            "How the reference pictures align with the target video — <Picture 1> (from [Shot 2]) aligns with the 6.25-second mark of the target video."
        ))

    def test_shot_numbers_must_be_consecutive_and_unique(self) -> None:
        with self.assertRaisesRegex(ApiError, "consecutive order"):
            compile_prompt("[Shot 1] Start. [Shot 3] End.", mode="text")
        with self.assertRaisesRegex(ApiError, "consecutive order"):
            compile_prompt("[Shot 1] Start. [Shot 1] Repeat.", mode="text")


if __name__ == "__main__":
    unittest.main()
