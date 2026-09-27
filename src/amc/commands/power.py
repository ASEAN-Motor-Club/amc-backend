"""In-game /power commands — same surface as the Discord /power cog.

Discord source of truth: amc_cogs/power_calc.py. The in-game variants use
identical subcommands and identical defaults, with the differences the game
chat medium requires:

- no autocomplete: part ids are typed exactly (the /power parts + /power
  guide output tell players where to find ids)
- /power recommend arguments follow a FIXED SEQUENCE: target hp, minimum
  engine weight, maximum engine weight, induction branch (any/na/turbo/eco/ev)
- no limit choice: recommend always shows the 15 closest builds
- no graph: the torque/power curve chart is Discord-only

Rendering follows the house popup conventions (see amc-command-registry
skill): headings as bare <Bold> outside <Small>, body inside <Small>, one tag
per span closed with </>, no emoji.
"""

import asyncio

from amc.command_framework import registry, CommandContext
from django.utils.translation import gettext as _, gettext_lazy
from powercalc import (
    PartNotFound,
    compute_setup,
    data_version,
    list_parts,
    model_version,
    provenance,
    search,
)

# Discord's app_commands.choices mapping, kept identical
_BRANCH_VALUES = {
    "any": "all",
    "all": "all",
    "na": "na",
    "turbo": "turbo",
    "eco": "eco",
    "ev": "ev",
}

_RECOMMEND_LIMIT = 15  # always; in-game has no limit argument


def _normalize_branch(token: str) -> str | None:
    return _BRANCH_VALUES.get(token.strip().lower())


def _setup_text(result) -> str:
    if result.is_ev:
        return _(
            "<Title>EV power</>"
            "\n\n<Bold>{part}</> ({asset})"
            "\n\n<Small>Peak power: {hp} hp (fixed motor rating)\n"
            "EVs have no intake/turbo options.</>"
        ).format(
            part=result.engine_part,
            asset=result.engine_asset,
            hp=f"{result.peak_power_hp:.1f}",
        )

    lines = [
        _("Peak power: {hp} hp @ {rpm} rpm").format(
            hp=f"{result.peak_power_hp:.1f}",
            rpm=f"{result.peak_power_rpm:,.0f}",
        ),
        _("Peak torque: {nm} Nm @ {rpm} rpm").format(
            nm=f"{result.peak_torque_nm:.1f}",
            rpm=f"{result.peak_torque_rpm:,.0f}",
        ),
        _("Engine: {part} ({asset})").format(
            part=result.engine_part, asset=result.engine_asset
        ),
    ]
    if result.mass_kg is not None:
        lines.append(_("Engine mass: {kg} kg").format(kg=f"{result.mass_kg:.0f}"))
    lines.append(
        _("Intake: {intake}").format(
            intake=result.intake_part or _("stock")
        )
    )
    lines.append(
        _("Induction: {turbo}").format(
            turbo=result.turbo_part or _("naturally aspirated")
        )
    )
    if result.cost is not None:
        lines.append(_("Part-row cost: ${cost}").format(cost=f"{result.cost:,}"))
    msg = _("<Title>Engine dyno result</>\n\n") + "\n".join(lines)
    msg += (
        _("\n\n<Small>model {model} · data {data} · in-game dyno validated</>")
    ).format(model=model_version(), data=data_version())
    return msg


def _recommend_text(target_hp: float, hits) -> str:
    if not hits:
        return _(
            "<Title>No builds near {hp} hp</>"
            "\n\n<Small>No engine/intake/turbo combination peaks within "
            "tolerance of that target. Widen the target, change the "
            "induction branch, or relax the weight filters.</>"
        ).format(hp=f"{target_hp:g}")

    lines = []
    for h in hits:
        intake = h.intake_part or _("stock intake")
        turbo = h.turbo_part or _("no turbo")
        cat = f" · {h.category}" if h.category else ""
        lines.append(
            _("{hp} hp @ {rpm} — {nm} Nm — {engine} + {intake} + {turbo}{cat} — ${cost}")
            .format(
                hp=f"{h.peak_power_hp:.1f}",
                rpm=f"{h.peak_power_rpm:,.0f}",
                nm=f"{h.peak_torque_nm:.0f}",
                engine=h.engine_part,
                intake=intake,
                turbo=turbo,
                cat=cat,
                cost=f"{h.cost:,}",
            )
        )
    msg = _("<Title>Builds near {hp} hp ({count} shown, closest first)</>\n\n").format(
        hp=f"{target_hp:g}", count=len(hits)
    )
    msg += "<Small>" + "\n".join(lines) + "</>"
    msg += (
        _("\n\n<Secondary>Use /power setup <engine> [intake] [turbo] to dyno one "
          "of these builds.</>")
    )
    return msg


@registry.register(
    "/power",
    description=gettext_lazy(
        "Engine power calculator guide (setup, recommend, parts, version)"
    ),
    category="Vehicle Management",
)
async def cmd_power(ctx: CommandContext):
    """Guide: what every /power subcommand does."""
    msg = _(
        "<Title>Power Calculator</>"
        "\n\n<Bold>/power setup</>"
        "\n<Small>Dyno an exact engine build and get peak hp/torque and rpm. "
        "Takes the engine part id, then optionally an intake and a "
        "turbocharger id. Omit the intake for stock and the turbo for "
        "naturally aspirated.</>"
        "\n\n<Bold>/power recommend</>"
        "\n<Small>Finds the 15 builds closest to a target hp. Arguments follow "
        "a fixed sequence: target hp, minimum engine weight, maximum engine "
        "weight, induction type (any, na, turbo, eco, ev). Weight pair and "
        "branch are optional; weights must be given together.</>"
        "\n\n<Bold>/power parts</>"
        "\n<Small>Lists every intake and turbocharger part id with its values "
        "— use it to find the ids for /power setup.</>"
        "\n\n<Bold>/power version</>"
        "\n<Small>Shows the calculator model and data versions, and how the "
        "model validates against the in-game dyno.</>"
        "\n\n<Secondary>Part ids are typed exactly, no autocomplete.</>"
    )
    await ctx.reply(msg)


@registry.register(
    ["/power setup"],
    description=gettext_lazy("Dyno an exact engine + intake + turbo build"),
    category="Vehicle Management",
)
async def cmd_power_setup_usage(ctx: CommandContext):
    await ctx.reply(
        _(
            "<Title>Usage</>"
            "\n\n<Bold>/power setup <engine> [intake] [turbo]</>"
            "\n\n<Small>Omit the intake for stock and the turbo for naturally "
            "aspirated. Part ids are typed exactly — /power parts lists "
            "intake and turbo ids.</>"
        )
    )


@registry.register(
    ["/power setup"],
    description=gettext_lazy("Dyno an exact engine + intake + turbo build"),
    category="Vehicle Management",
)
async def cmd_power_setup(
    ctx: CommandContext,
    engine: str,
    intake: str | None = None,
    turbo: str | None = None,
):
    if intake is not None and turbo is None and intake.lower() in _BRANCH_VALUES:
        # "/power setup <engine> na" is almost certainly a misplaced recommend
        # branch, not an intake id — say so instead of an opaque PartNotFound.
        await ctx.reply(
            _(
                "<Title>Unknown part</>"
                "\n\n<Small>That second argument looks like an induction type "
                "(na/turbo/eco/ev), but setup takes part ids. Did you mean "
                "/power recommend?</>"
            )
        )
        return
    try:
        result = await asyncio.to_thread(compute_setup, engine, intake, turbo)
    except PartNotFound as e:
        await ctx.reply(
            _(
                "<Title>Unknown part</>"
                "\n\n<Small>{error} — use /power parts for intake and turbo "
                "ids.</>"
            ).format(error=str(e))
        )
        return
    await ctx.reply(_setup_text(result))


@registry.register(
    ["/power recommend"],
    description=gettext_lazy(
        "Show the 15 builds closest to a target hp "
        "(hp, [min_weight max_weight], [branch])"
    ),
    category="Vehicle Management",
)
async def cmd_power_recommend_usage(ctx: CommandContext):
    await ctx.reply(
        _(
            "<Title>Usage</>"
            "\n\n<Bold>/power recommend <hp> [min_weight max_weight] [branch]</>"
            "\n\n<Small>Shows the 15 builds closest to the target hp. The "
            "argument sequence is fixed: target hp, then optionally a "
            "minimum AND maximum engine weight pair, then optionally the "
            "induction type: any, na, turbo, eco, ev.</>"
        )
    )


@registry.register(
    ["/power recommend"],
    description=gettext_lazy(
        "Show the 15 builds closest to a target hp "
        "(hp, [min_weight max_weight], [branch])"
    ),
    category="Vehicle Management",
)
async def cmd_power_recommend(
    ctx: CommandContext,
    target_hp: int,
    min_weight: int | None = None,
    max_weight: int | None = None,
    branch: str | None = None,
):
    if (min_weight is None) != (max_weight is None):
        await ctx.reply(
            _(
                "<Title>Weight filter</>"
                "\n\n<Small>Give minimum AND maximum engine weight together, "
                "or neither.</>"
            )
        )
        return
    branch_value = None
    if branch is not None:
        branch_value = _normalize_branch(branch)
        if branch_value is None:
            await ctx.reply(
                _(
                    "<Title>Unknown induction</>"
                    "\n\n<Small>'{branch}' is not an induction type. Use one "
                    "of: any, na, turbo, eco, ev.</>"
                ).format(branch=branch)
            )
            return
    hits = await asyncio.to_thread(
        search,
        float(target_hp),
        tolerance=4.0,
        branch=branch_value,
        min_mass=min_weight,
        max_mass=max_weight,
        limit=_RECOMMEND_LIMIT,
    )
    await ctx.reply(_recommend_text(float(target_hp), hits))


@registry.register(
    ["/power parts"],
    description=gettext_lazy("List intake and turbocharger part values"),
    category="Vehicle Management",
)
async def cmd_power_parts(ctx: CommandContext):
    parts = await asyncio.to_thread(list_parts)
    lines = [_("Intakes (slope / base rpm ratio)")]
    for pid, p in sorted(parts.get("Intake", {}).items()):
        i = p["intake"]
        lines.append(
            _("{pid} — {slope:+g} from {base}").format(
                pid=pid,
                slope=i["Slope"],
                base=i["BaseRPMRatio"],
            )
        )
    lines.append("")
    lines.append(_("Turbos (torque multiplier)"))
    for pid, p in sorted(parts.get("Turbocharger", {}).items()):
        t = p["turbocharger"]
        note = " (reduced hp)" if "Eco" in pid else ""
        lines.append(
            _("{pid} — x{mult:g}{note}").format(
                pid=pid, mult=t.get("TorqueMultiplier", 1.0), note=note
            )
        )
    msg = (
        _("<Title>Induction parts</>\n\n")
        + "<Small>" + "\n".join(lines) + "</>"
    )
    await ctx.reply(msg)


@registry.register(
    ["/power version"],
    description=gettext_lazy("Calculator model/data versions"),
    category="Vehicle Management",
)
async def cmd_power_version(ctx: CommandContext):
    v = provenance().get("validation", {})
    ig, mo = v.get("in_game", {}), v.get("model", {})
    desc = _(
        "model {model} · data {data}\n"
        "validated against in-game dyno ({method}):\n"
        "in-game {ig_t} Nm @{ig_tr} / {ig_p} hp @{ig_pr}\n"
        "model   {mo_t} Nm @{mo_tr} / {mo_p} hp @{mo_pr}"
    ).format(
        model=model_version(),
        data=data_version(),
        method=v.get("method", ""),
        ig_t=ig.get("peak_torque_nm"),
        ig_tr=ig.get("peak_torque_rpm"),
        ig_p=ig.get("peak_power_hp"),
        ig_pr=ig.get("peak_power_rpm"),
        mo_t=mo.get("peak_torque_nm"),
        mo_tr=mo.get("peak_torque_rpm"),
        mo_p=mo.get("peak_power_hp"),
        mo_pr=mo.get("peak_power_rpm"),
    )
    await ctx.reply(_("<Title>powercalc</>\n\n<Small>{desc}</>").format(desc=desc))