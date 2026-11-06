"""Versioned prompt templates. Only validated enum values are interpolated."""

PROMPT_VERSION = "same_angle_v1"

STYLE_SCENES = {
    "white_studio": "a seamless bright white infinity-cove studio with a soft grey floor gradient",
    "dark_luxury": "a dark charcoal studio with dramatic rim lighting and a glossy black floor",
    "showroom": "a modern premium car showroom with polished concrete floor and soft architectural lighting",
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

RELIGHT_TEMPLATE = (
    "This image is a real car photo composited onto {scene}. Only harmonize lighting, reflections and "
    "the contact shadow so it looks photographed in that studio. Do not change the car's shape, color, "
    "wheels, badges, plate or any detail."
)


class UnknownStyle(ValueError):
    pass


def scene_for(style: str) -> str:
    try:
        return STYLE_SCENES[style]
    except KeyError:
        raise UnknownStyle(style) from None


def same_angle_prompt(style: str) -> str:
    return f"{SYSTEM_INSTRUCTION}\n\n{SAME_ANGLE_TEMPLATE.format(scene=scene_for(style))}"
