"""Versioned prompt templates. Only validated enum values are interpolated."""

PROMPT_VERSION = "same_angle_v1"
PROMPT_VERSION_CLEAN = "same_angle_v2_clean"
PROMPT_VERSION_ANGLE = "novel_angle_v1"

STYLE_SCENES = {
    "white_studio": "a seamless bright white infinity-cove studio with a soft grey floor gradient",
    "dark_luxury": "a dark charcoal studio with dramatic rim lighting and a glossy black floor",
    "showroom": "a modern premium car showroom with polished concrete floor and soft architectural lighting",
}

ANGLE_DESCRIPTIONS = {
    "front": "straight-on front view, camera at headlight height, centred on the grille",
    "front_three_quarter_left": "front three-quarter view showing the front and the left side of the car",
    "front_three_quarter_right": "front three-quarter view showing the front and the right side of the car",
    "left_side": "full left side profile, camera perpendicular to the car",
    "right_side": "full right side profile, camera perpendicular to the car",
    "rear_three_quarter_left": "rear three-quarter view showing the back and the left side of the car",
    "rear_three_quarter_right": "rear three-quarter view showing the back and the right side of the car",
    "rear": "straight-on rear view, camera at tail-light height",
}

SYSTEM_INSTRUCTION = (
    "You are an automotive studio photo editor. Use the supplied image as the primary source of "
    "vehicle identity. Preserve visible make, model, body shape, paint, trim, grille, lamps, wheels, "
    "accessories and visible condition. Do not add or remove damage, logos, badges, accessories or "
    "license-plate content. Do not invent unsupported hidden details."
)

SAME_ANGLE_TEMPLATE = (
    "Transform this vehicle photograph into realistic premium studio photography. Keep the exact "
    "original camera view, framing and vehicle geometry. Scene: {scene}. Lighting: soft professional "
    "automotive softboxes, realistic reflections and contact shadows. Keep the vehicle's observed "
    "features and condition unchanged. No redesign, no cosmetic repairs, no added text or watermarks."
)

OVERLAY_CLEANUP = (
    "The source may contain things that are not part of the real scene: phone screenshot buttons or "
    "icons, status bars, timestamps, captions, emojis, stickers or other graphics laid over the photo. "
    "Remove all of them completely and show the car and studio cleanly behind where they were. "
    "Keep text that is physically on the car itself (badges, model names) exactly as it is."
)

NOVEL_ANGLE_TEMPLATE = (
    "Using ALL supplied reference photos of the SAME vehicle, create one photorealistic studio photograph "
    "of that exact vehicle from this viewpoint: {angle}. Scene: {scene}. Lighting: soft professional "
    "automotive softboxes, realistic reflections and contact shadows. Match the make, model, body shape, "
    "paint colour, wheels, lights, trim and visible condition shown in the references. Where a part of the "
    "car is not visible in any reference, keep it plain and consistent with the visible design; do not "
    "invent distinctive badges, accessories, damage or stickers. Render the number plate blank. "
    "No text, logos or watermarks in the image."
)

RELIGHT_TEMPLATE = (
    "This image is a real car photo composited onto {scene}. Only harmonize lighting, reflections and "
    "the contact shadow so it looks photographed in that studio. Do not change the car's shape, color, "
    "wheels, badges, plate or any detail."
)


class UnknownStyle(ValueError):
    pass


class UnknownAngle(ValueError):
    pass


def scene_for(style: str) -> str:
    try:
        return STYLE_SCENES[style]
    except KeyError:
        raise UnknownStyle(style) from None


def same_angle_prompt(style: str, *, cleanup: bool = False) -> str:
    prompt = f"{SYSTEM_INSTRUCTION}\n\n{SAME_ANGLE_TEMPLATE.format(scene=scene_for(style))}"
    if cleanup:
        prompt += f"\n\n{OVERLAY_CLEANUP}"
    return prompt


def novel_angle_prompt(style: str, angle: str) -> str:
    try:
        desc = ANGLE_DESCRIPTIONS[angle]
    except KeyError:
        raise UnknownAngle(angle) from None
    return (f"{SYSTEM_INSTRUCTION}\n\n{NOVEL_ANGLE_TEMPLATE.format(angle=desc, scene=scene_for(style))}"
            f"\n\n{OVERLAY_CLEANUP}")
