"""/power — Motor Town engine power/torque calculator (vanilla + MoreTuning).

Thin Discord layer over the powercalc package (see src/powercalc/). Two modes:

- /power setup: calculate an exact engine + intake + turbo combination
- /power recommend: target-hp recommendations across all combinations
- /power parts: intake/turbo reference values
- /power version: model/data version + validation case

All heavy computation runs via asyncio.to_thread -- the cog shares the
event loop with the game-chat relay and must never block it.
"""

from __future__ import annotations

import asyncio
import io
import os

import discord
from discord import app_commands
from discord.ext import commands

from powercalc import (
    PartNotFound,
    compute_setup,
    data_version,
    list_engines,
    list_parts,
    model_version,
    provenance,
    search,
)
from powercalc.model import KW_TO_HP

_BRANCH_CHOICES = [
    app_commands.Choice(name="any induction", value="all"),
    app_commands.Choice(name="naturally aspirated (no turbo)", value="na"),
    app_commands.Choice(name="turbo", value="turbo"),
    app_commands.Choice(name="eco turbo (reduced hp)", value="eco"),
    app_commands.Choice(name="electric", value="ev"),
]

_COLOR_OK = discord.Color.green()
_COLOR_INFO = discord.Color.blurple()
_COLOR_ERR = discord.Color.red()


def _fmt_intake(pid: str | None) -> str:
    return f"{pid} (stock)" if pid is None else pid


def _fmt_turbo(pid: str | None) -> str:
    return f"{pid} (naturally aspirated)" if pid is None else pid


def _setup_embed(result) -> discord.Embed:
    if result.is_ev:
        body = (
            f"**{result.engine_part}** (`{result.engine_asset}`)\n"
            f"Peak power: **{result.peak_power_hp:.1f} hp** (fixed motor rating)\n"
            f"EVs have no intake/turbo options."
        )
        return discord.Embed(title="EV power", description=body, color=_COLOR_INFO)

    lines = [
        f"**Peak power:** {result.peak_power_hp:.1f} hp @ {result.peak_power_rpm:,.0f} rpm",
        f"**Peak torque:** {result.peak_torque_nm:.1f} Nm @ {result.peak_torque_rpm:,.0f} rpm",
        f"**Engine:** {result.engine_part} (`{result.engine_asset}`)",
    ]
    if result.mass_kg is not None:
        lines.append(f"**Engine mass:** {result.mass_kg:.0f} kg")
    lines.append(f"**Intake:** {_fmt_intake(result.intake_part)}")
    lines.append(f"**Induction:** {_fmt_turbo(result.turbo_part)}")
    if result.cost is not None:
        lines.append(f"**Part-row cost:** ${result.cost:,}")
    emb = discord.Embed(
        title="Engine dyno result",
        description="\n".join(lines),
        color=_COLOR_OK,
    )
    emb.set_footer(
        text=f"model {model_version()} · data {data_version()} · in-game dyno validated <0.2%"
    )
    return emb


def _curve_png(result) -> io.BytesIO:
    """Render torque/power vs rpm as a compact PNG chart for Discord.

    matplotlib is already a project dependency (imported elsewhere in the
    worker), so this adds no new package. Two stacked axes share the rpm
    scale; each series is annotated with its peak. Styled for Discord's
    dark surface with an explicit panel color so it stays readable on
    light theme too. Runs via asyncio.to_thread (Agg, no display).
    """
    # Best effort: a stable config dir avoids the per-process font-cache
    # rebuild matplotlib otherwise does when HOME is not writable. Only
    # effective if matplotlib has not been imported yet this process.
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl-amc-cache")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    pts = result.curve_points
    rpm = [p[0] for p in pts]
    panel, text, gridc = "#2b2d31", "#dbdee1", "#4b4d54"
    fig, (ax_t, ax_p) = plt.subplots(
        2, 1, figsize=(5.2, 3.6), dpi=100, sharex=True
    )
    fig.patch.set_facecolor(panel)
    for ax, series, color, unit, peak_v, peak_r in (
        (ax_t, [p[1] for p in pts], "#5865f2", "Nm",
         result.peak_torque_nm, result.peak_torque_rpm),
        (ax_p, [p[2] * KW_TO_HP for p in pts], "#f0b232", "hp",
         result.peak_power_hp, result.peak_power_rpm),
    ):
        ax.set_facecolor(panel)
        ax.plot(rpm, series, color=color, lw=1.6)
        ax.scatter([peak_r], [peak_v], color=color, s=12, zorder=3)
        ax.annotate(
            f"{peak_v:.0f} {unit} @ {peak_r:,.0f}",
            (peak_r, peak_v),
            xytext=(4, 2),
            textcoords="offset points",
            color=color,
            fontsize=7,
        )
        ax.set_ylabel(unit, color=text, fontsize=8)
        ax.tick_params(colors=text, labelsize=7)
        ax.grid(True, color=gridc, lw=0.4, alpha=0.6)
        for spine in ax.spines.values():
            spine.set_color(gridc)
    ax_p.set_xlabel("rpm", color=text, fontsize=8)
    ax_p.set_xlim(0, pts[-1][0])
    fig.suptitle(result.engine_part, color=text, fontsize=9)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=panel)
    plt.close(fig)
    buf.seek(0)
    return buf


def _recommend_embed(target_hp: float, hits) -> discord.Embed:
    if not hits:
        return discord.Embed(
            title=f"No builds near {target_hp:g} hp",
            description="Widen the tolerance, change the branch, or relax the mass filters.",
            color=_COLOR_ERR,
        )
    lines = []
    for h in hits:
        intake = h.intake_part or "stock intake"
        turbo = h.turbo_part or "no turbo"
        cat = f" · {h.category}" if h.category else ""
        lines.append(
            f"**{h.peak_power_hp:.1f} hp** @ {h.peak_power_rpm:,.0f} — "
            f"{h.peak_torque_nm:.0f} Nm — "
            f"`{h.engine_part}` + {intake} + {turbo}{cat} — ${h.cost:,}"
        )
    emb = discord.Embed(
        title=f"Builds near {target_hp:g} hp ({len(hits)} shown, closest first)",
        description="\n".join(lines)[:4000],
        color=_COLOR_OK,
    )
    emb.set_footer(
        text=f"model {model_version()} · data {data_version()} · /power setup <parts> for one build"
    )
    return emb


def _filter_choices(
    items: list[tuple[str, str]], current: str
) -> list[app_commands.Choice[str]]:
    cur = (current or "").strip().lower()
    if cur:
        matched = [it for it in items if cur in it[0].lower() or cur in it[1].lower()]
    else:
        matched = items
    return [
        app_commands.Choice(name=name[:100], value=value[:100])
        for value, name in matched[:25]
    ]


async def _engine_autocomplete(interaction: discord.Interaction, current: str):
    engines = [
        (
            e["engine_part"],
            f"{e['engine_part']} ({e['engine_asset']}, {e['mass_kg']:.0f}kg)",
        )
        for e in list_engines()
    ]
    return _filter_choices(engines, current)


async def _intake_autocomplete(interaction: discord.Interaction, current: str):
    parts = list_parts().get("Intake", {})
    items = [
        (
            pid,
            f"{pid} — slope {p['intake']['Slope']:+.2f} from {p['intake']['BaseRPMRatio']}",
        )
        for pid, p in sorted(parts.items())
    ]
    return _filter_choices(items, current)


async def _turbo_autocomplete(interaction: discord.Interaction, current: str):
    parts = list_parts().get("Turbocharger", {})
    items = []
    for pid, p in sorted(parts.items()):
        t = p["turbocharger"]
        label = f"{pid} — x{t.get('TorqueMultiplier', 1.0):g}"
        if "Eco" in pid:
            label += " (reduced hp)"
        items.append((pid, label))
    return _filter_choices(items, current)


class PowerCalcCog(commands.Cog):
    """Slash commands exposing the powercalc library."""

    power = app_commands.Group(
        name="power",
        description="Motor Town engine power/torque calculator (vanilla + MoreTuning)",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------ autocomplete
    @power.command(
        name="setup",
        description="Calculate an exact engine + intake + turbo combination",
    )
    @app_commands.describe(
        engine="Engine part id",
        intake="Intake part (omit for stock intake)",
        turbo="Turbocharger part (omit for naturally aspirated)",
        show_curve="Attach a torque/power chart image",
    )
    @app_commands.autocomplete(
        engine=_engine_autocomplete,
        intake=_intake_autocomplete,
        turbo=_turbo_autocomplete,
    )
    async def power_setup(
        self,
        interaction: discord.Interaction,
        engine: str,
        intake: str | None = None,
        turbo: str | None = None,
        show_curve: bool = False,
    ):
        try:
            result = await asyncio.to_thread(compute_setup, engine, intake, turbo, True)
        except PartNotFound as e:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Unknown part",
                    description=f"`{e}` — use the autocomplete suggestions or `/power parts`.",
                    color=_COLOR_ERR,
                ),
                ephemeral=True,
            )
            return
        emb = _setup_embed(result)
        file = None
        if show_curve and not result.is_ev:
            buf = await asyncio.to_thread(_curve_png, result)
            file = discord.File(buf, filename="curve.png")
            emb.set_image(url="attachment://curve.png")
        if file is not None:
            await interaction.response.send_message(embed=emb, file=file)
        else:
            await interaction.response.send_message(embed=emb)

    @power.command(name="recommend", description="Recommend builds near a target hp")
    @app_commands.describe(
        target_hp="Target peak horsepower",
        branch="Induction type (default: any)",
        min_mass="Minimum engine mass in kg",
        max_mass="Maximum engine mass in kg",
        limit="How many results to show (1-15, default 10)",
    )
    @app_commands.choices(branch=_BRANCH_CHOICES)
    async def power_recommend(
        self,
        interaction: discord.Interaction,
        target_hp: app_commands.Range[int, 5, 5000],
        branch: app_commands.Choice[str] | None = None,
        min_mass: app_commands.Range[int, 0, 5000] | None = None,
        max_mass: app_commands.Range[int, 0, 5000] | None = None,
        limit: app_commands.Range[int, 1, 15] = 10,
    ):
        await interaction.response.defer()
        hits = await asyncio.to_thread(
            search,
            float(target_hp),
            tolerance=4.0,
            branch=branch.value if branch else None,
            min_mass=float(min_mass) if min_mass is not None else None,
            max_mass=float(max_mass) if max_mass is not None else None,
            limit=limit,
        )
        await interaction.followup.send(
            embed=_recommend_embed(float(target_hp), hits),
        )

    @power.command(name="parts", description="List intake and turbocharger part values")
    async def power_parts(self, interaction: discord.Interaction):
        parts = await asyncio.to_thread(list_parts)
        lines = ["**Intakes** (slope / base rpm ratio)"]
        for pid, p in sorted(parts.get("Intake", {}).items()):
            i = p["intake"]
            lines.append(f"`{pid}` — {i['Slope']:+g} from {i['BaseRPMRatio']}")
        lines.append("\n**Turbos** (torque multiplier)")
        for pid, p in sorted(parts.get("Turbocharger", {}).items()):
            t = p["turbocharger"]
            note = " (reduced hp)" if "Eco" in pid else ""
            lines.append(f"`{pid}` — x{t.get('TorqueMultiplier', 1.0):g}{note}")
        emb = discord.Embed(
            title="Induction parts",
            description="\n".join(lines)[:4000],
            color=_COLOR_INFO,
        )
        emb.set_footer(text=f"data {data_version()}")
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @power.command(name="version", description="Calculator model/data versions")
    async def power_version(self, interaction: discord.Interaction):
        v = provenance().get("validation", {})
        ig, mo = v.get("in_game", {}), v.get("model", {})
        desc = (
            f"model `{model_version()}` · data `{data_version()}`\n"
            f"validated against in-game dyno ({v.get('method', '')}):\n"
            f"in-game {ig.get('peak_torque_nm')} Nm @{ig.get('peak_torque_rpm')} / "
            f"{ig.get('peak_power_hp')} hp @{ig.get('peak_power_rpm')}\n"
            f"model   {mo.get('peak_torque_nm')} Nm @{mo.get('peak_torque_rpm')} / "
            f"{mo.get('peak_power_hp')} hp @{mo.get('peak_power_rpm')}"
        )
        await interaction.response.send_message(
            embed=discord.Embed(title="powercalc", description=desc, color=_COLOR_INFO),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PowerCalcCog(bot))
