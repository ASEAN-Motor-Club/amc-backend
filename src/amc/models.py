import math
import asyncstdlib as a
from datetime import timedelta
from deepdiff import DeepHash
from django.contrib import admin
from django.contrib.gis.db import models
from django.db.models import (
    Q,
    F,
    Sum,
    Max,
    Window,
    Count,
    When,
    Case,
    OuterRef,
    Subquery,
    Exists,
)
from django.db.models.functions import RowNumber, Lead, Lag
from django.db.models.lookups import GreaterThan, GreaterThanOrEqual
from decimal import Decimal
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.postgres.fields import ArrayField
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model
from django.contrib.postgres.fields import DateTimeRangeField
from django.contrib.postgres.search import SearchVector
from django.contrib.postgres.indexes import GinIndex
from typing import override, final, ClassVar, TYPE_CHECKING, Optional
from amc.server_logs import (
    PlayerVehicleLogEvent,
    PlayerEnteredVehicleLogEvent,
    PlayerExitedVehicleLogEvent,
    PlayerBoughtVehicleLogEvent,
    PlayerSoldVehicleLogEvent,
)
from amc.mod_server import spawn_dealership
from amc.enums import CargoKey, VehicleKey

User = get_user_model()


class PlayerQuerySet(models.QuerySet):
    def with_total_session_time(self):
        return self.annotate(
            total_session_time=Sum(
                "characters__status_logs__duration", default=timedelta(0)
            )
        )

    def with_last_login(self):
        return self.annotate(
            last_login=Max(
                "characters__status_logs__timespan__startswith", default=None
            )
        )


@final
class PlayerManager(models.Manager.from_queryset(PlayerQuerySet)):  # type: ignore[misc]
    pass


@final
class Player(models.Model):
    unique_id = models.PositiveBigIntegerField(primary_key=True)
    discord_user_id = models.PositiveBigIntegerField(unique=True, null=True, blank=True)
    discord_name = models.CharField(max_length=200, null=True, blank=True)
    user = models.OneToOneField(
        User, models.SET_NULL, related_name="player", null=True, blank=True
    )
    suspect = models.BooleanField(default=False)
    adminstrator = models.BooleanField(default=False)
    displayer = models.BooleanField(
        default=False, help_text="Livery artists, showcase etc"
    )
    social_score = models.IntegerField(default=0)
    language = models.CharField(
        max_length=10,
        default="en-gb",
        choices=[("en-gb", "English"), ("id", "Indonesian")],
    )
    notes = models.TextField(blank=True)

    if TYPE_CHECKING:
        characters: "CharacterManager"
        team_memberships: models.Manager["TeamMembership"]
        teams_owned: models.Manager["Team"]
        teams: models.Manager["Team"]
        scheduled_events: "ScheduledEventManager"
        outbox_messages: models.Manager["PlayerMailMessage"]
        inbox_messages: models.Manager["PlayerMailMessage"]
        contracts_signed: models.Manager["ServerSignContractLog"]
        passengers_delivered: models.Manager["ServerPassengerArrivedLog"]
        tow_requests_delivered: models.Manager["ServerTowRequestArrivedLog"]
        tickets: models.Manager["Ticket"]
        tickets_issued: models.Manager["Ticket"]
        decals: models.Manager["VehicleDecal"]
        shifts: models.Manager["PlayerShift"]
        rescue_responses: models.Manager["RescueRequest"]
        ministry_terms: models.Manager["MinistryTerm"]
        elections_won: models.Manager["MinistryElection"]
        ministry_candidacies: models.Manager["MinistryCandidacy"]
        ministry_votes: models.Manager["MinistryVote"]
        delivered_cargos: models.Manager["ServerCargoArrivedLog"]
        character_names: list[str]
        characters_count: int

    objects: ClassVar[PlayerManager] = PlayerManager()

    @override
    def __str__(self) -> str:
        if self.discord_name:
            return self.discord_name
        character = self.characters.first()
        if character is None:
            return str(self.unique_id)
        return f"{character.name} {self.unique_id}"

    @property
    @admin.display(
        description="Whether user is verified",
        boolean=True,
    )
    def verified(self):
        return self.discord_user_id is not None

    async def get_latest_character(self):
        character = await (
            self.characters.with_last_login()
            .filter(last_login__isnull=False)
            .alatest("last_login")
        )
        return character


class CharacterQuerySet(models.QuerySet):
    def with_total_session_time(self):
        return self.annotate(
            total_session_time=Sum("status_logs__duration", default=timedelta(0))
        )

    def with_last_login(self):
        return self.annotate(
            last_login=Max(
                "status_logs__timespan__startswith",
            )
        )


@final
class CharacterManager(models.Manager.from_queryset(CharacterQuerySet)):  # type: ignore[misc]
    if TYPE_CHECKING:
        model: type["Character"]

        def get_queryset(self) -> CharacterQuerySet: ...

    async def aget_or_create_character_player(
        self, player_name, player_id, character_guid=None
    ):
        """
        Gets or creates a character and its associated player.

        This method handles multiple identification scenarios:
        1. Adding a GUID to a character that was previously created without one.
        2. Changing the name of a character identified by its GUID.
        3. Finding a character by name, even if it now has a GUID.
        4. Creating new characters with or without a GUID.
        """
        player, player_created = await Player.objects.aget_or_create(
            unique_id=player_id
        )
        assert character_guid != self.model.INVALID_GUID, "Invalid character id"

        if character_guid:
            # A GUID is provided. First, attempt to "claim" a character that matches the name
            # but currently has no GUID. This handles the 'test_add_guid' case.
            try:
                await (
                    self.get_queryset()
                    .filter(player=player, name=player_name, guid__isnull=True)
                    .aupdate(guid=character_guid)
                )
            except Exception:
                pass

            # Now, use aupdate_or_create with the GUID as the definitive lookup key.
            # This will find the character (either pre-existing or the one just updated)
            # and update its name if it has changed, or create a new character if none exists.
            character, character_created = await self.get_queryset().aupdate_or_create(
                guid=character_guid, player=player, defaults={"name": player_name}
            )
        else:
            # No GUID provided. We look up by name.
            # Use filter instead of aget_or_create to handle potential duplicates.
            # Prefer characters with GUIDs (more authoritative).
            character = await (
                self.get_queryset()
                .filter(name=player_name, player=player)
                .order_by(F("guid").asc(nulls_last=True), "-id")
                .afirst()
            )
            if character:
                character_created = False
            else:
                character = await self.get_queryset().acreate(
                    name=player_name, player=player, guid=None
                )
                character_created = True

        return (character, player, character_created, player_created)


@final
class Character(models.Model):
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="characters"
    )
    guid = models.CharField(max_length=32, unique=True, editable=False, null=True)
    name = models.CharField(max_length=200)
    custom_name = models.CharField(max_length=200, null=True, blank=True)
    money = models.PositiveIntegerField(null=True, blank=True)
    # levels
    driver_level = models.PositiveIntegerField(null=True, blank=True)
    bus_level = models.PositiveIntegerField(null=True, blank=True)
    taxi_level = models.PositiveIntegerField(null=True, blank=True)
    police_level = models.PositiveIntegerField(null=True, blank=True)
    truck_level = models.PositiveIntegerField(null=True, blank=True)
    wrecker_level = models.PositiveIntegerField(null=True, blank=True)
    racer_level = models.PositiveIntegerField(null=True, blank=True)
    saving_rate = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[
            MinValueValidator(Decimal("0.00")),
            MaxValueValidator(Decimal("1.00")),
        ],
    )
    loan_repayment_rate = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[
            MinValueValidator(Decimal("0.00")),
            MaxValueValidator(Decimal("1.00")),
        ],
    )
    rp_mode = models.BooleanField(default=False)
    reject_ubi = models.BooleanField(default=False)
    ubi_multiplier = models.FloatField(default=1.0)
    # Cached from CharacterLocation — updated by monitor_locations
    last_location = models.PointField(srid=0, dim=3, null=True, blank=True)
    last_vehicle_key = models.CharField(max_length=100, null=True, blank=True)
    last_online = models.DateTimeField(null=True, blank=True)
    # Set to timezone.now() by _check_shortcut_zones on zone entry,
    # consumed/cleared by process_events; auto-expires after 1 hour.
    shortcut_zone_entered_at = models.DateTimeField(null=True, blank=True)

    total_donations = models.PositiveBigIntegerField(default=0)

    # Credit score: 0–200, default 100 (neutral). Affects loan fees.
    credit_score = models.IntegerField(default=100)

    # Government Employee
    gov_employee_until = models.DateTimeField(null=True, blank=True)
    gov_employee_level = models.PositiveIntegerField(default=0)
    gov_employee_contributions = models.PositiveBigIntegerField(default=0)

    # Criminal
    criminal_laundered_total = models.PositiveBigIntegerField(default=0)

    # Police
    police_confiscated_total = models.PositiveBigIntegerField(default=0)

    # Wealth tax crossover DM — sent once when tax > interest, 30-day cooldown
    crossover_warning_sent_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[CharacterManager] = CharacterManager()

    if TYPE_CHECKING:
        team_memberships: models.Manager["TeamMembership"]
        game_events: models.Manager["GameEvent"]
        bot_invocation_logs: models.Manager["BotInvocationLog"]
        song_request_logs: models.Manager["SongRequestLog"]
        status_logs: models.Manager["PlayerStatusLog"]
        chat_logs: models.Manager["PlayerChatLog"]
        restock_depot_logs: models.Manager["PlayerRestockDepotLog"]
        vehicle_logs: models.Manager["PlayerVehicleLog"]
        last_login: Optional[timezone.datetime]
        total_session_time: Optional[timedelta]

    INVALID_GUID = "00000000000000000000000000000000"

    @property
    def is_gov_employee(self):
        return (
            self.gov_employee_until is not None
            and self.gov_employee_until > timezone.now()
        )

    @override
    def __str__(self):
        return f"{self.name} ({self.player.unique_id})"

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(saving_rate__gte=0) & Q(saving_rate__lte=1),
                name="saving_rate_between_0_1",
            )
        ]


@final
class CriminalRecord(models.Model):
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="criminal_records"
    )
    reason = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["character", "expires_at"])]

    @override
    def __str__(self):
        return f"{self.character.name} — {self.reason} (expires {self.expires_at})"

    @classmethod
    async def aget_active(cls, character=None):
        qs = cls.objects.filter(expires_at__gt=timezone.now())
        if character:
            qs = qs.filter(character=character)
        return [r async for r in qs.select_related("character")]


@final
class Confiscation(models.Model):
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="confiscations_received",
        null=True, blank=True
    )
    officer = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="confiscations_made",
        null=True, blank=True
    )
    cargo_key = models.CharField(max_length=100)
    amount = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["character", "created_at"])]

    @override
    def __str__(self):
        return f"{self.officer.name} confiscated {self.cargo_key} (${self.amount:,}) from {self.character.name}"


class FactionChoice(models.TextChoices):
    COP = "cop", "Cop"
    CRIMINAL = "criminal", "Criminal"


@final
class FactionMembership(models.Model):
    player = models.OneToOneField(
        Player, on_delete=models.CASCADE, related_name="faction_membership"
    )
    faction = models.CharField(max_length=10, choices=FactionChoice.choices)
    joined_at = models.DateTimeField(auto_now_add=True)
    last_switched_at = models.DateTimeField(null=True, blank=True)

    @override
    def __str__(self):
        return f"{self.player} — {self.get_faction_display()}"

    @property
    def cooldown_remaining(self) -> timedelta:
        """Return remaining cooldown duration, or zero if cooldown has elapsed."""
        from django.conf import settings

        if self.last_switched_at is None:
            return timedelta(0)
        cooldown = timedelta(
            hours=getattr(settings, "FACTION_SWITCH_COOLDOWN_HOURS", 24)
        )
        elapsed = timezone.now() - self.last_switched_at
        remaining = cooldown - elapsed
        return max(remaining, timedelta(0))

    @property
    def can_switch(self) -> bool:
        return self.cooldown_remaining == timedelta(0)


@final
class PoliceSession(models.Model):
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="police_sessions"
    )
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["character", "ended_at"])]

    @override
    def __str__(self):
        status = "active" if self.ended_at is None else f"ended {self.ended_at}"
        return f"{self.character.name} — {status}"


@final
class Team(models.Model):
    name = models.CharField(max_length=200)
    tag = models.CharField(max_length=6)
    description = models.TextField(blank=True)
    discord_thread_id = models.PositiveBigIntegerField(unique=True)
    owners = models.ManyToManyField(Player, related_name="teams_owned")
    logo = models.FileField(upload_to="team_logos", null=True, blank=True)
    bg_color = models.CharField(max_length=6, default="FFFFFF")
    text_color = models.CharField(max_length=6, default="000000")
    racing = models.BooleanField(default=True)

    players = models.ManyToManyField(
        Player, through="TeamMembership", related_name="teams"
    )

    @override
    def __str__(self):
        return f"[{self.tag}] {self.name}"


@final
class TeamMembership(models.Model):
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="team_memberships"
    )
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, null=True, related_name="team_memberships"
    )
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    date_joined = models.DateTimeField(default=timezone.now)

    @final
    class Meta:
        # This constraint ensures that a player can only be a member
        # of a specific team once.
        unique_together = ("player", "team")

    @override
    def __str__(self):
        return f"{self.player.discord_name} in {self.team.name}"


@final
class RaceSetup(models.Model):
    config = models.JSONField(null=True, blank=True)
    hash = models.CharField(max_length=200, unique=True)
    name = models.CharField(max_length=200, null=True)
    lateral_spacing = models.IntegerField(
        default=600, help_text="Horizonal spacing between starting grid"
    )
    longitudinal_spacing = models.IntegerField(
        default=1000, help_text="Vertical spacing between starting grid"
    )
    initial_offset = models.IntegerField(
        default=1000, help_text="Gap between starting line and first row"
    )
    pole_side_right = models.BooleanField(
        default=True, help_text="If true, the first position is on the right side"
    )
    reverse_starting_direction = models.BooleanField(
        default=False,
        help_text="If true, the starting grid will be on the opposite side of the starting line",
    )

    @staticmethod
    def calculate_hash(race_setup):
        hashes = DeepHash(race_setup)
        return hashes[race_setup]

    @override
    def __str__(self):
        try:
            if self.config is None:
                return "Unknown race setup (no config)"
            route_name = self.config["Route"]["RouteName"]
            num_laps = self.config["NumLaps"]
            return f"{route_name} ({num_laps} laps) - {self.hash[:8]}"
        except Exception:
            return "Unknown race setup"

    @property
    def route_name(self):
        if self.name is not None:
            return self.name
        if self.config is None:
            return None
        return self.config.get("Route", {}).get("RouteName")

    @property
    def num_laps(self):
        if self.config is None:
            return 0
        return self.config.get("NumLaps", 0)

    @property
    def vehicles(self):
        if self.config is None:
            return []
        return self.config.get("VehicleKeys", [])

    @property
    def engines(self):
        if self.config is None:
            return []
        return self.config.get("EngineKeys", [])

    @property
    def num_sections(self):
        return len(self.waypoints)

    @property
    def waypoints(self):
        if self.config is None:
            return []
        return self.config.get("Route", {}).get("Waypoints", [])


@final
class Championship(models.Model):
    name = models.CharField(max_length=200)
    discord_thread_id = models.CharField(
        max_length=32, null=True, blank=True, unique=True
    )
    description = models.TextField(blank=True)

    personal_prize_by_position = [
        4_800_000,
        2_400_000,
        1_440_000,
        960_000,
        720_000,
        480_000,
        360_000,
        300_000,
        300_000,
        240_000,
    ]
    team_prize_by_position = [
        2_700_000,
        1_500_000,
        900_000,
        600_000,
        300_000,
    ]

    @override
    def __str__(self):
        return self.name

    async def calculate_personal_prizes(self):
        personal_standings = ChampionshipPoint.objects.personal_standings(self.id)[
            : len(self.personal_prize_by_position)
        ]
        return [
            (
                await Character.objects.select_related("player").aget(
                    pk=standing["character_id"]
                ),
                self.personal_prize_by_position[i],
            )
            async for i, standing in a.builtins.enumerate(personal_standings)
        ]

    async def calculate_team_prizes(self):
        team_standings = ChampionshipPoint.objects.team_standings(self.id)[
            : len(self.team_prize_by_position)
        ]

        async def calculate_team_member_prizes(standing, total_team_prize):
            total_participations = await ChampionshipPoint.objects.filter(
                championship=self, team__id=standing["team__id"]
            ).acount()
            member_contributions = (
                ChampionshipPoint.objects.filter(
                    championship=self, team__id=standing["team__id"]
                )
                .values("participant__character")
                .annotate(
                    points=Count("id"), character_id=F("participant__character__id")
                )
            )
            return [
                (
                    await Character.objects.select_related("player").aget(
                        pk=member_contribution["character_id"]
                    ),
                    total_team_prize
                    * member_contribution["points"]
                    / total_participations,
                )
                async for member_contribution in member_contributions
            ]

        return [
            character_prize
            async for i, standing in a.builtins.enumerate(team_standings)
            for character_prize in await calculate_team_member_prizes(
                standing, self.team_prize_by_position[i]
            )
        ]


class ScheduledEventQuerySet(models.QuerySet):
    def filter_active_at(self, timestamp):
        return self.filter(
            start_time__lte=timestamp,
            end_time__gte=timestamp,
        )


@final
class ScheduledEventManager(models.Manager.from_queryset(ScheduledEventQuerySet)):  # type: ignore[misc]
    pass


@final
class ScheduledEvent(models.Model):
    name = models.CharField(max_length=200)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    discord_event_id = models.CharField(
        max_length=32, null=True, blank=True, unique=True
    )
    discord_thread_id = models.CharField(
        max_length=32, null=True, blank=True, unique=True
    )
    discord_message_id = models.CharField(
        max_length=32, null=True, blank=True, unique=True
    )
    race_setup = models.ForeignKey(
        RaceSetup, on_delete=models.SET_NULL, null=True, related_name="scheduled_events"
    )
    championship = models.ForeignKey(
        Championship,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scheduled_events",
    )
    players = models.ManyToManyField(
        Player, through="ScheduledEventPlayer", related_name="scheduled_events"
    )
    description = models.TextField(blank=True)
    description_in_game = models.TextField(
        blank=True,
        help_text="This will be shown when players use /events. Defaults to description",
    )
    time_trial = models.BooleanField(default=False)
    staggered_start_delay = models.PositiveIntegerField(
        default=0,
        help_text="Delay between staggered start, in seconds. This can be overridden in the game",
    )
    objects: ClassVar[ScheduledEventManager] = ScheduledEventManager()

    @override
    def __str__(self):
        return self.name


@final
class ScheduledEventPlayer(models.Model):
    player = models.ForeignKey(Player, on_delete=models.CASCADE)
    scheduled_event = models.ForeignKey(ScheduledEvent, on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["player", "scheduled_event"],
                name="unique_player_scheduled_event",
            )
        ]


@final
class GameEvent(models.Model):
    name = models.CharField(max_length=200)
    guid = models.CharField(max_length=32, db_index=True, editable=False)
    start_time = models.DateTimeField(editable=False, auto_now_add=True)
    last_updated = models.DateTimeField(editable=False, auto_now=True)
    scheduled_event = models.ForeignKey(
        ScheduledEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="game_events",
    )
    race_setup = models.ForeignKey(
        RaceSetup, on_delete=models.SET_NULL, null=True, related_name="game_events"
    )
    state = models.IntegerField()
    discord_message_id = models.PositiveBigIntegerField(null=True)
    owner = models.ForeignKey(Character, models.SET_NULL, null=True, blank=True)

    characters = models.ManyToManyField(
        Character, through="GameEventCharacter", related_name="game_events"
    )

    @override
    def __str__(self):
        return self.name


class ParticipantQuerySet(models.QuerySet):
    def filter_best_time_per_player(self):
        return (
            self.annotate(
                attempts_count=Window(
                    expression=Count("id", filter=Q(finished=True)),
                    partition_by=[F("character")],
                )
            )
            .alias(
                p_rank=Window(
                    expression=RowNumber(),
                    partition_by=[F("character")],
                    order_by=[
                        F("disqualified").asc(),
                        F("finished").desc(),
                        F("net_time").asc(),
                    ],
                ),
            )
            .filter(p_rank=1)
        )

    def filter_by_scheduled_event(self, scheduled_event):
        if scheduled_event.time_trial:
            criteria = Q(
                Q(game_event__scheduled_event=scheduled_event)
                | Q(game_event__race_setup=scheduled_event.race_setup),
                game_event__start_time__gte=scheduled_event.start_time,
                game_event__start_time__lte=scheduled_event.end_time,
            )
        else:
            criteria = Q(game_event__scheduled_event=scheduled_event)
        return self.filter(criteria)

    def filter_by_track(self, track):
        return self.filter(game_event__race_setup=track)

    def results_for_scheduled_event(self, scheduled_event):
        return (
            self.select_related(
                "character",
                "character__player",
                "championship_point",
                "championship_point__team",
            )
            .filter_by_scheduled_event(scheduled_event)
            .filter_best_time_per_player()
            .order_by(
                "disqualified",
                "wrong_engine",
                "wrong_vehicle",
                "-finished",
                "laps",
                "section_index",
                "net_time",
            )
        )

    def results_for_track(self, track):
        return (
            self.select_related(
                "character",
                "character__player",
                "championship_point",
                "championship_point__team",
            )
            .filter(finished=True, disqualified=False)
            .filter_by_track(track)
            .filter_best_time_per_player()
            .order_by(
                "disqualified",
                "wrong_engine",
                "wrong_vehicle",
                "-finished",
                "laps",
                "section_index",
                "net_time",
            )
        )


@final
class ParticipantManager(models.Manager.from_queryset(ParticipantQuerySet)):  # type: ignore[misc]
    pass


@final
class GameEventCharacter(models.Model):
    character = models.ForeignKey(Character, on_delete=models.CASCADE)
    game_event = models.ForeignKey(
        GameEvent, on_delete=models.CASCADE, related_name="participants"
    )
    rank = models.IntegerField()  # raw game value
    laps = models.IntegerField(default=0)
    section_index = models.IntegerField(default=-1)
    first_section_total_time_seconds = models.FloatField(null=True, blank=True)
    last_section_total_time_seconds = models.FloatField(null=True, blank=True)
    penalty_seconds = models.FloatField(default=0)
    net_time = models.GeneratedField(
        expression=F("last_section_total_time_seconds")
        - F("first_section_total_time_seconds")
        + F("penalty_seconds"),
        output_field=models.FloatField(null=True, blank=True),
        db_persist=True,
    )
    best_lap_time = models.FloatField(null=True, blank=True)
    lap_times = ArrayField(  # raw game value
        models.FloatField(),
        default=list,
        null=True,
        blank=True,
    )
    wrong_engine = models.BooleanField(default=False)
    wrong_vehicle = models.BooleanField(default=False)
    disqualified = models.BooleanField(default=False)
    finished = models.BooleanField(default=False)
    objects: ClassVar[ParticipantManager] = ParticipantManager()

    class Meta:
        ordering = ["disqualified", "-finished", "-laps", "-section_index", "net_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["character", "game_event"], name="unique_character_game_event"
            )
        ]


class ChampionshipPointQuerySet(models.QuerySet):
    def personal_standings(request, championship_id):
        return (
            ChampionshipPoint.objects.filter(
                championship=championship_id,
            )
            .values("participant__character")
            .annotate(
                total_points=Sum("points"),
                player_id=F("participant__character__player__unique_id"),
                character_id=F("participant__character__id"),
                character_name=F("participant__character__name"),
            )
            .order_by("-total_points")
        )

    def team_standings(request, championship_id):
        top_results_subquery = (
            ChampionshipPoint.objects.select_related("team")
            .filter(
                championship=championship_id,
                team__isnull=False,
            )
            .annotate(
                team_pos=Window(
                    expression=RowNumber(),
                    partition_by=[
                        F("team"),
                        F("participant__game_event__scheduled_event"),
                    ],
                    order_by=[F("points").desc()],
                )
            )
            .filter(team_pos__lte=2)
        )
        return (
            ChampionshipPoint.objects.filter(pk__in=top_results_subquery.values("pk"))
            .values("team__id", "team__tag", "team__name")
            .annotate(total_points=Sum("points"))
            .order_by("-total_points")
        )


@final
class ChampionshipPointManager(models.Manager.from_queryset(ChampionshipPointQuerySet)):  # type: ignore[misc]
    pass


@final
class ChampionshipPoint(models.Model):
    championship = models.ForeignKey(Championship, models.SET_NULL, null=True)
    participant = models.OneToOneField(
        GameEventCharacter, models.CASCADE, related_name="championship_point"
    )
    team = models.ForeignKey(Team, models.SET_NULL, null=True, blank=True)
    points = models.PositiveIntegerField(default=0, blank=True)
    prize = models.PositiveIntegerField(default=0, blank=True)

    objects: ClassVar[ChampionshipPointManager] = ChampionshipPointManager()

    event_prize_by_position = [
        900000,
        540000,
        360000,
        300000,
        240000,
        180000,
        150000,
        120000,
        105000,
        105000,
    ]
    time_trial_prize_by_position = [
        300000,
        180000,
        120000,
        100000,
        80000,
        60000,
        50000,
        40000,
        35000,
        35000,
    ]
    event_points_by_position = [25, 20, 16, 13, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    time_trial_points_by_position = [10, 8, 6, 5, 4, 3, 2, 1]

    @classmethod
    def get_event_points_for_position(self, position: int, time_trial: bool = False):
        try:
            if time_trial:
                return self.time_trial_points_by_position[position]
            return self.event_points_by_position[position]
        except IndexError:
            return 0

    @classmethod
    def get_time_trial_points_for_position(self, position: int):
        try:
            return self.time_trial_points_by_position[position]
        except IndexError:
            return 0

    @classmethod
    def get_event_prize_for_position(
        self, position: int, time_trial: bool = False, base_pay=50_000
    ):
        try:
            if time_trial:
                return self.time_trial_prize_by_position[position] + base_pay
            return self.event_prize_by_position[position] + base_pay
        except IndexError:
            return base_pay


class LapSectionTimeQuerySet(models.QuerySet):
    def annotate_net_time(self):
        return self.annotate(
            net_time=F("total_time_seconds")
            - F("game_event_character__first_section_total_time_seconds")
        )

    def annotate_deltas(self):
        return self.annotate(
            section_duration=Window(
                expression=Lead("total_time_seconds"),
                partition_by=[F("game_event_character")],
                order_by=[F("lap").asc(), F("section_index").asc()],
            ),
        )


@final
class LapSectionTimeManager(models.Manager.from_queryset(LapSectionTimeQuerySet)):  # type: ignore[misc]
    pass


@final
class LapSectionTime(models.Model):
    game_event_character = models.ForeignKey(
        GameEventCharacter, on_delete=models.CASCADE, related_name="lap_section_times"
    )
    section_index = models.IntegerField()
    lap = models.IntegerField()
    rank = models.IntegerField()
    total_time_seconds = models.FloatField()
    objects: ClassVar[LapSectionTimeManager] = LapSectionTimeManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["game_event_character", "section_index", "lap"],
                name="unique_event_lap_section_time",
            )
        ]


@final
class Vehicle(models.Model):
    id = models.PositiveBigIntegerField(primary_key=True)
    name = models.CharField(max_length=200)

    @override
    def __str__(self):
        return f"{self.name} ({self.id})"


@final
class Company(models.Model):
    name = models.CharField(max_length=200)
    description = models.CharField(max_length=250, blank=True)
    owner = models.ForeignKey(Character, on_delete=models.CASCADE)
    is_corp = models.BooleanField()
    first_seen_at = models.DateTimeField(blank=True)
    money = models.IntegerField(null=True, blank=True)
    guid = models.CharField(max_length=32, null=True, blank=True)
    has_tp_permission = models.BooleanField(default=False)

    class Meta:
        verbose_name = _("Company")
        verbose_name_plural = _("Companies")

    @override
    def __str__(self):
        return f"{self.name} ({self.id})"


@final
class ServerLog(models.Model):
    timestamp = models.DateTimeField()
    log_path = models.CharField(max_length=500, null=True)
    hostname = models.CharField(max_length=100, default="asean-mt-server")
    tag = models.CharField(max_length=100, default="mt-server")
    text = models.TextField()
    event_processed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["timestamp", "text"], name="unique_event_log_entry"
            )
        ]
        indexes = [
            GinIndex(
                SearchVector("text", config="english"),
                name="log_text_search_idx",
            )
        ]


@final
class BotInvocationLog(models.Model):
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="bot_invocation_logs"
    )
    timestamp = models.DateTimeField()
    prompt = models.TextField()


@final
class SongRequestLog(models.Model):
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="song_request_logs"
    )
    timestamp = models.DateTimeField()
    song = models.TextField()


@final
class PlayerStatusLog(models.Model):
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="status_logs"
    )
    timespan = DateTimeRangeField()
    duration = models.GeneratedField(
        expression=F("timespan__endswith") - F("timespan__startswith"),
        output_field=models.DurationField(),
        db_persist=True,
    )
    original_log = models.ForeignKey(ServerLog, on_delete=models.CASCADE, null=True)

    @property
    def login_time(self):
        return self.timespan.lower

    @property
    def logout_time(self):
        return self.timespan.upper

    class Meta:
        ordering = ["-timespan__startswith"]


@final
class PlayerChatLog(models.Model):
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="chat_logs"
    )
    timestamp = models.DateTimeField()
    text = models.TextField()

    class Meta:
        indexes = [
            GinIndex(
                SearchVector("text", config="english"),
                name="chat_text_search_idx",
            )
        ]


@final
class PlayerRestockDepotLog(models.Model):
    timestamp = models.DateTimeField()
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="restock_depot_logs"
    )
    depot_name = models.CharField(max_length=200)


@final
class PlayerVehicleLog(models.Model):
    class Action(models.TextChoices):
        ENTERED = "EN", _("Entered")
        EXITED = "EX", _("Exited")
        BOUGHT = "BO", _("Bought")
        SOLD = "SO", _("Sold")

    timestamp = models.DateTimeField()
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="vehicle_logs"
    )
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, null=True)
    vehicle_game_id = models.PositiveBigIntegerField(null=True, db_index=True)
    vehicle_name = models.CharField(max_length=100, null=True)
    action = models.CharField(max_length=2, choices=Action)

    @classmethod
    def action_for_event(cls, event: PlayerVehicleLogEvent):
        match event:
            case PlayerEnteredVehicleLogEvent():
                return cls.Action.ENTERED
            case PlayerExitedVehicleLogEvent():
                return cls.Action.EXITED
            case PlayerBoughtVehicleLogEvent():
                return cls.Action.BOUGHT
            case PlayerSoldVehicleLogEvent():
                return cls.Action.SOLD
            case _:
                raise ValueError("Unknown vehicle log event")

    class Meta:
        ordering = ["-timestamp"]
        constraints = [
            models.UniqueConstraint(
                fields=["timestamp", "character", "vehicle", "action"],
                name="unique_vehicle_log_entry",
            )
        ]


@final
class CharacterLocationManager(models.Manager):
    def filter_character_activity(self, character, start_time, end_time):
        return self.filter(
            character=character, timestamp__gte=start_time, timestamp__lt=end_time
        ).annotate(
            prev_location=Window(
                expression=Lag("location"),
                partition_by=[F("character")],
                order_by=[F("timestamp").asc()],
            )
        )

    def filter_characters_activity(self, characters, start_time, end_time):
        """Batch version: fetch location rows for multiple characters in one query."""
        return (
            self.filter(
                character__in=characters,
                timestamp__gte=start_time,
                timestamp__lt=end_time,
            )
            .annotate(
                prev_location=Window(
                    expression=Lag("location"),
                    partition_by=[F("character")],
                    order_by=[F("timestamp").asc()],
                )
            )
            .order_by("character_id", "timestamp")
        )


@final
class CharacterLocation(models.Model):
    timestamp = models.DateTimeField(db_index=True, auto_now_add=True)
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="locations"
    )
    location = models.PointField(srid=0, dim=3)
    vehicle_key = models.CharField(max_length=100, null=True, choices=VehicleKey)
    objects: ClassVar[CharacterLocationManager] = CharacterLocationManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["timestamp", "character"], name="unique_character_location"
            )
        ]
        indexes = [
            models.Index(
                fields=["character", "-timestamp"], name="charloc_char_ts_idx"
            ),
        ]

    @classmethod
    async def get_character_activity(
        self,
        character,
        start_time,
        end_time,
        afk_treshold=1000,
        teleport_treshold=10000,
    ):
        qs = self.objects.filter_character_activity(character, start_time, end_time)
        if not await qs.aexists():
            return (False, False)

        total_dis = 0
        async for cl in qs:
            if cl.prev_location is None:
                continue
            dis = cl.prev_location.distance(cl.location)
            if dis > teleport_treshold:
                continue
            total_dis += dis
            if total_dis > afk_treshold:
                return (True, True)
        return (True, False)

    @classmethod
    async def batch_get_character_activity(
        cls,
        characters,
        start_time,
        end_time,
        afk_threshold=1000,
        teleport_threshold=10000,
    ):
        """
        Batch activity check for multiple characters in a single query.
        Returns dict[character_id, (is_online, is_active)].
        """
        qs = cls.objects.filter_characters_activity(characters, start_time, end_time)

        # Accumulate distance per character
        totals: dict[int, float] = {}
        result: dict[int, tuple[bool, bool]] = {}

        # Mark all queried characters as offline by default
        for c in characters:
            result[c.id] = (False, False)

        async for cl in qs:
            char_id = cl.character_id
            if char_id not in totals:
                totals[char_id] = 0.0
                result[char_id] = (True, False)  # online but not yet proven active

            if cl.prev_location is None:
                continue

            dis = cl.prev_location.distance(cl.location)
            if dis > teleport_threshold:
                continue

            totals[char_id] += dis
            if totals[char_id] > afk_threshold:
                result[char_id] = (True, True)

        return result


@final
class PlayerMailMessage(models.Model):
    from_player = models.ForeignKey(
        Player, models.CASCADE, related_name="outbox_messages", null=True, blank=True
    )
    to_player = models.ForeignKey(Player, models.CASCADE, related_name="inbox_messages")
    content = models.TextField()
    sent_at = models.DateTimeField(editable=False, auto_now_add=True)
    received_at = models.DateTimeField(editable=False, null=True, blank=True)


@final
class DeliveryPoint(models.Model):
    guid = models.CharField(max_length=200, primary_key=True)
    name = models.CharField(max_length=200)
    type = models.CharField(max_length=200, blank=True, default="")
    coord = models.PointField(srid=3857, dim=3)
    data = models.JSONField(null=True, blank=True)
    last_updated = models.DateTimeField(editable=False, auto_now=True, null=True)
    removed = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} ({self.type})"

    class Meta:
        ordering = ["name"]


class DeliveryPointStorageQuerySet(models.QuerySet):
    def annotate_default_capacity(self):
        return self.annotate(
            capacity_normalized=Case(
                When(capacity__isnull=False, then=F("capacity")),
                default=Subquery(
                    DeliveryPointStorage.objects.filter(
                        delivery_point=OuterRef("delivery_point"),
                        cargo=OuterRef("cargo__type"),
                        kind=OuterRef("kind"),
                    ).values("capacity")
                ),
            )
        )


@final
class DeliveryPointStorageManager(
    models.Manager.from_queryset(
        DeliveryPointStorageQuerySet
    )  # pyrefly: ignore [invalid-inheritance]
):  # type: ignore[misc]
    pass


class DeliveryPointStorage(models.Model):
    class Kind(models.TextChoices):
        INPUT = "IN", "Input"
        OUTPUT = "OU", "Output"

    delivery_point = models.ForeignKey(
        DeliveryPoint, models.CASCADE, related_name="storages"
    )
    kind = models.CharField(max_length=2, choices=Kind)
    cargo_key = models.CharField(max_length=200, db_index=True, choices=CargoKey)
    cargo = models.ForeignKey(
        "Cargo", models.CASCADE, related_name="storages", null=True
    )
    amount = models.PositiveIntegerField()
    capacity = models.PositiveIntegerField(null=True, blank=True)
    objects: ClassVar[DeliveryPointStorageManager] = DeliveryPointStorageManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["delivery_point", "kind", "cargo_key"],
                name="unique_delivery_point_storage",
            ),
        ]


@final
class CharacterAFKReminder(models.Model):
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="afk_reminders"
    )
    destination = models.PointField(srid=0, dim=3)
    created_at = models.DateTimeField(editable=False, auto_now_add=True)


@final
class Delivery(models.Model):
    timestamp = models.DateTimeField()
    character = models.ForeignKey(
        Character, on_delete=models.SET_NULL, null=True, related_name="deliveries"
    )
    cargo_key = models.CharField(max_length=200, db_index=True, choices=CargoKey)
    quantity = models.PositiveIntegerField()
    payment = models.PositiveBigIntegerField()
    subsidy = models.PositiveBigIntegerField(default=0)
    rp_mode = models.BooleanField(default=False)
    sender_point = models.ForeignKey(
        "DeliveryPoint",
        models.SET_NULL,
        null=True,
        blank=True,
        related_name="batch_deliveries_out",
    )
    destination_point = models.ForeignKey(
        "DeliveryPoint",
        models.SET_NULL,
        null=True,
        blank=True,
        related_name="batch_deliveries_in",
    )
    job = models.ForeignKey(
        "DeliveryJob", models.SET_NULL, null=True, blank=True, related_name="deliveries"
    )


@final
class ServerCargoArrivedLog(models.Model):
    timestamp = models.DateTimeField()
    player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, related_name="delivered_cargos"
    )
    character = models.ForeignKey(
        Character, on_delete=models.SET_NULL, null=True, related_name="delivered_cargos"
    )
    cargo_key = models.CharField(max_length=200, db_index=True, choices=CargoKey)
    payment = models.PositiveBigIntegerField()
    weight = models.FloatField(null=True, blank=True)
    damage = models.FloatField(null=True, blank=True)
    sender_point = models.ForeignKey(
        "DeliveryPoint",
        models.SET_NULL,
        null=True,
        blank=True,
        related_name="deliveries_out",
    )
    destination_point = models.ForeignKey(
        "DeliveryPoint",
        models.SET_NULL,
        null=True,
        blank=True,
        related_name="deliveries_in",
    )
    data = models.JSONField(null=True, blank=True)


@final
class ServerSignContractLog(models.Model):
    timestamp = models.DateTimeField()
    guid = models.CharField(max_length=32, db_index=True, editable=False, null=True)
    player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, related_name="contracts_signed"
    )
    cargo_key = models.CharField(max_length=200, db_index=True)
    amount = models.FloatField()
    finished_amount = models.FloatField(default=0)
    cost = models.PositiveIntegerField()
    payment = models.PositiveIntegerField()
    delivered = models.BooleanField(default=False)
    data = models.JSONField(null=True, blank=True)


@final
class ServerPassengerArrivedLog(models.Model):
    class PassengerType(models.IntegerChoices):
        Unknown = (0,)
        Hitchhiker = (1,)
        Taxi = (2,)
        Ambulance = (3,)
        Bus = (4,)

    timestamp = models.DateTimeField()
    player = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        related_name="passengers_delivered",
    )
    passenger_type = models.IntegerField(db_index=True, choices=PassengerType)
    distance = models.FloatField()
    payment = models.PositiveIntegerField()
    arrived = models.BooleanField(default=True)
    comfort = models.BooleanField(null=True)
    urgent = models.BooleanField(null=True)
    limo = models.BooleanField(null=True)
    offroad = models.BooleanField(null=True)
    comfort_rating = models.IntegerField(null=True)
    urgent_rating = models.IntegerField(null=True)
    data = models.JSONField(null=True, blank=True)


@final
class ServerTowRequestArrivedLog(models.Model):
    timestamp = models.DateTimeField()
    player = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        related_name="tow_requests_delivered",
    )
    payment = models.PositiveIntegerField()
    data = models.JSONField(null=True, blank=True)


@final
class PolicePatrolLog(models.Model):
    timestamp = models.DateTimeField()
    player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, related_name="police_patrols"
    )
    patrol_point_id = models.IntegerField()
    base_payment = models.IntegerField(default=0)
    area_bonus_payment = models.IntegerField(default=0)
    data = models.JSONField(null=True, blank=True)


@final
class PolicePenaltyLog(models.Model):
    timestamp = models.DateTimeField()
    player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, related_name="police_penalties"
    )
    warning_only = models.BooleanField()
    data = models.JSONField(null=True, blank=True)


@final
class PoliceShiftLog(models.Model):
    class Action(models.TextChoices):
        START = "START", "Started Shift"
        END = "END", "Ended Shift"

    timestamp = models.DateTimeField()
    player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, related_name="police_shifts"
    )
    action = models.CharField(max_length=5, choices=Action)
    data = models.JSONField(null=True, blank=True)


@final
class TeleportPoint(models.Model):
    name = models.CharField(max_length=20)
    character = models.ForeignKey(
        Character,
        on_delete=models.SET_NULL,
        related_name="teleport_points",
        null=True,
        blank=True,
    )
    location = models.PointField(srid=0, dim=3)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["name", "character"], name="unique_character_teleport_point"
            )
        ]


@final
class VehicleDealership(models.Model):
    vehicle_key = models.CharField(max_length=100, null=True, choices=VehicleKey)
    location = models.PointField(srid=0, dim=3)
    yaw = models.FloatField()
    spawn_on_restart = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    async def spawn(self, http_client_mod):
        await spawn_dealership(
            http_client_mod,
            self.vehicle_key,
            {"X": self.location.x, "Y": self.location.y, "Z": self.location.z},
            self.yaw,
        )


@final
class Garage(models.Model):
    config = models.JSONField(null=True, blank=True)
    hostname = models.CharField(max_length=128)
    spawn_on_restart = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    tag = models.CharField(max_length=256, null=True, blank=True)


@final
class Thank(models.Model):
    sender_character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="thanks_given"
    )
    recipient_character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="thanks_received"
    )
    timestamp = models.DateTimeField()


@final
class Cargo(models.Model):
    key = models.CharField(max_length=200, primary_key=True)
    label = models.CharField(max_length=200)
    type = models.ForeignKey(
        "self", models.SET_NULL, null=True, blank=True, related_name="subtypes"
    )

    def __str__(self):
        return self.label


class DeliveryJobQuerySet(models.QuerySet):
    def filter_active(self):
        now = timezone.now()
        return self.filter(fulfilled=False, requested_at__lte=now, expired_at__gte=now)

    def annotate_active(self):
        now = timezone.now()
        return self.annotate(
            active=~F("fulfilled")
            & GreaterThan(now, F("requested_at"))
            & GreaterThan(F("expired_at"), now)
        )

    def exclude_has_conflicting_active_job(self):
        return self.exclude(
            Exists(
                self.model.objects.filter_active().filter(
                    Q(cargo_key=OuterRef("cargo_key"))
                    | Q(cargos__in=OuterRef("cargos")),
                    Q(source_points__in=OuterRef("source_points"))
                    | Q(destination_points__in=OuterRef("destination_points")),
                )
            )
        )

    def exclude_recently_posted(self, hours_since=12):
        return self.exclude(
            Exists(
                self.model.objects.filter(
                    name=OuterRef("name"),
                    expired_at__gte=timezone.now() - timedelta(hours=hours_since),
                    template=False,
                )
            )
        )

    def filter_by_delivery(self, delivery_source, delivery_destination, cargo_key):
        return self.filter(
            Q(source_points=delivery_source) | Q(source_points=None),
            Q(destination_points=delivery_destination) | Q(destination_points=None),
            Q(cargo_key=cargo_key)
            | Q(cargos__key=cargo_key)
            | Q(cargos__type__key=cargo_key)
            | Exists(
                Cargo.objects.filter(
                    key=cargo_key,
                    type__in=OuterRef("cargos"),
                )
            ),
        )


class DeliveryJobTemplateQuerySet(models.QuerySet):
    def exclude_has_conflicting_active_job(self):
        # Exclude templates that:
        # 1. Already have an active job created from them, OR
        # 2. Have overlapping cargos AND overlapping source/destination points with any active job
        return self.exclude(
            Exists(
                DeliveryJob.objects.filter_active().filter(
                    Q(created_from=OuterRef("pk"))
                    | Q(
                        Q(cargos__in=OuterRef("cargos")),
                        Q(source_points__in=OuterRef("source_points"))
                        | Q(destination_points__in=OuterRef("destination_points")),
                    )
                )
            )
        )

    def exclude_recently_posted(self, hours_since=12):
        # Matches logic in DeliveryJobQuerySet.exclude_recently_posted but adapted for created_from
        return self.exclude(
            Exists(
                DeliveryJob.objects.filter(
                    created_from=OuterRef("pk"),
                    requested_at__gte=timezone.now() - timedelta(hours=hours_since),
                )
            )
        )


@final
class DeliveryJobTemplateManager(
    models.Manager.from_queryset(
        DeliveryJobTemplateQuerySet
    )  # pyrefly: ignore [invalid-inheritance]
):  # type: ignore[misc]
    pass


@final
class DeliveryJobTemplate(models.Model):
    name = models.CharField(max_length=200, help_text="Give the template a name")
    description = models.TextField(blank=True, null=True)
    cargos = models.ManyToManyField(
        "Cargo",
        related_name="job_templates",
        blank=True,
        help_text="Use either Cargo Key or this field for multiple cargo types",
    )
    source_points = models.ManyToManyField(
        "DeliveryPoint", related_name="job_templates_out", blank=True
    )
    destination_points = models.ManyToManyField(
        "DeliveryPoint", related_name="job_templates_in", blank=True
    )

    default_quantity = models.PositiveIntegerField(
        help_text="Default quantity requested"
    )
    bonus_multiplier = models.FloatField(default=1.0)
    completion_bonus = models.PositiveIntegerField(default=50000)
    rp_mode = models.BooleanField(
        default=False, help_text="Requires the job to be done in RP mode"
    )
    enabled = models.BooleanField(
        default=True, help_text="Disabled templates are skipped during job posting"
    )

    # Template specific settings
    expected_player_count_for_quantity = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="When player count is lower than this, quantity will be scaled down",
    )
    job_posting_probability = models.FloatField(
        default=1.0,
        help_text="The probability at which the job is posted. Defaults to 100% (1.0)",
    )
    duration_hours = models.FloatField(
        default=5.0, help_text="The number of hours to complete the job"
    )

    # Adaptive posting equilibrium (updated automatically)
    success_score = models.FloatField(
        default=1.0,
        help_text="Adaptive posting multiplier [0.1-2.0]. Boosted on completion, decayed on expiry.",
    )
    lifetime_completions = models.PositiveIntegerField(
        default=0, help_text="Total jobs completed from this template (observability)"
    )
    lifetime_expirations = models.PositiveIntegerField(
        default=0, help_text="Total jobs expired from this template (observability)"
    )

    objects: ClassVar[DeliveryJobTemplateManager] = DeliveryJobTemplateManager()

    def __str__(self):
        return self.name


@final
class MinistryTerm(models.Model):
    minister = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="ministry_terms"
    )
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    initial_budget = models.DecimalField(max_digits=16, decimal_places=2)
    current_budget = models.DecimalField(max_digits=16, decimal_places=2)
    total_spent = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    # Anti-Embezzlement Audit
    created_jobs_count = models.IntegerField(default=0)
    expired_jobs_count = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(start_date__lt=F("end_date")),
                name="ministry_term_start_before_end",
            )
        ]

    @override
    def __str__(self):
        return f"Ministry Term ({self.start_date.date()} - {self.end_date.date()})"


@final
class MinistryElection(models.Model):
    class Phase(models.TextChoices):
        CANDIDACY = "CANDIDACY", "Candidacy"
        POLLING = "POLLING", "Polling"
        FINALIZED = "FINALIZED", "Finalized"

    created_at = models.DateTimeField(auto_now_add=True)
    candidacy_end_at = models.DateTimeField()
    poll_end_at = models.DateTimeField()
    winner = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="elections_won",
    )
    term_created = models.OneToOneField(
        "MinistryTerm",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="election",
    )
    is_processed = models.BooleanField(default=False)

    if TYPE_CHECKING:
        candidates: models.Manager["MinistryCandidacy"]
        votes: models.Manager["MinistryVote"]

    @property
    def phase(self):
        now = timezone.now()
        if self.winner_id or self.term_created:
            return self.Phase.FINALIZED
        if now < self.candidacy_end_at:
            return self.Phase.CANDIDACY
        if now < self.poll_end_at:
            return self.Phase.POLLING
        return self.Phase.FINALIZED

    def __str__(self):
        return f"Ministry Election {self.id} ({self.phase.label})"


@final
class MinistryCandidacy(models.Model):
    election = models.ForeignKey(
        MinistryElection, on_delete=models.CASCADE, related_name="candidates"
    )
    candidate = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="ministry_candidacies"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    manifesto = models.TextField(blank=True)

    if TYPE_CHECKING:
        votes: models.Manager["MinistryVote"]

    class Meta:
        unique_together = ("election", "candidate")
        verbose_name_plural = "Ministry Candidacies"


@final
class MinistryVote(models.Model):
    election = models.ForeignKey(
        MinistryElection, on_delete=models.CASCADE, related_name="votes"
    )
    voter = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="ministry_votes"
    )
    candidate = models.ForeignKey(
        MinistryCandidacy, on_delete=models.CASCADE, related_name="votes"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("election", "voter")


@final
class DeliveryJobManager(models.Manager.from_queryset(DeliveryJobQuerySet)):  # type: ignore[misc]
    pass


@final
class DeliveryJob(models.Model):
    name = models.CharField(
        max_length=200,
        null=True,
        help_text="Give the job a name so it can be identified",
    )
    cargo_key = models.CharField(
        max_length=200, db_index=True, choices=CargoKey, null=True, blank=True
    )  # deprecated, use `cargos` instead
    quantity_requested = models.PositiveIntegerField()
    quantity_fulfilled = models.PositiveIntegerField(default=0)
    requested_at = models.DateTimeField(auto_now_add=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True, editable=False)
    expired_at = models.DateTimeField(
        null=True, blank=True, help_text="Required for non-template jobs"
    )
    bonus_multiplier = models.FloatField()
    completion_bonus = models.PositiveIntegerField(default=0)
    cargos = models.ManyToManyField(
        "Cargo",
        related_name="jobs",
        blank=True,
        help_text="Use either Cargo Key or this field for multiple cargo types",
    )
    source_points = models.ManyToManyField(
        "DeliveryPoint", related_name="jobs_out", blank=True
    )
    destination_points = models.ManyToManyField(
        "DeliveryPoint", related_name="jobs_in", blank=True
    )
    discord_message_id = models.PositiveBigIntegerField(
        null=True, blank=True, help_text="For bot use only, leave blank"
    )
    description = models.TextField(blank=True, null=True)
    rp_mode = models.BooleanField(
        default=False, help_text="Requires the job to be done in RP mode"
    )
    created_from = models.ForeignKey(
        DeliveryJobTemplate, models.SET_NULL, null=True, blank=True, related_name="jobs"
    )

    # Ministry Funding
    funding_term = models.ForeignKey(
        MinistryTerm, models.SET_NULL, null=True, blank=True, related_name="funded_jobs"
    )
    escrowed_amount = models.PositiveIntegerField(
        default=0, help_text="Amount sequestered from Ministry budget"
    )

    fulfilled = models.GeneratedField(
        expression=GreaterThanOrEqual(F("quantity_fulfilled"), F("quantity_requested")),
        output_field=models.BooleanField(),
        db_persist=True,
    )

    if TYPE_CHECKING:
        bonus_percentage: int

    objects: ClassVar[DeliveryJobManager] = DeliveryJobManager()

    def __str__(self):
        return f"{self.name} ({self.id})"

    async def is_postable(self):
        job = self
        cargos = job.cargos.all()
        source_points = job.source_points.all()
        destination_points = job.destination_points.all()

        non_type_cargos = [c for c in cargos if "T::" not in c.key]
        destination_storages = DeliveryPointStorage.objects.filter(
            Q(cargo__in=non_type_cargos) | Q(cargo__type__in=cargos),
            delivery_point__in=destination_points,
        ).annotate_default_capacity()
        source_storages = DeliveryPointStorage.objects.filter(
            Q(cargo=job.cargo_key)
            | Q(cargo__in=non_type_cargos)
            | Q(cargo__type__in=cargos),
            delivery_point__in=source_points,
        ).annotate_default_capacity()

        destination_storage_capacities = [
            (storage.amount, storage.capacity_normalized or 0)
            async for storage in destination_storages
        ]
        source_storage_capacities = [
            (storage.amount, storage.capacity_normalized or 0)
            async for storage in source_storages
        ]
        destination_amount = sum(
            [amount for amount, capacity in destination_storage_capacities]
        )
        destination_capacity = sum(
            [capacity for amount, capacity in destination_storage_capacities]
        )
        source_amount = sum([amount for amount, capacity in source_storage_capacities])
        source_capacity = sum(
            [capacity for amount, capacity in source_storage_capacities]
        )

        quantity_requested = job.quantity_requested

        if destination_capacity == 0:
            is_destination_empty = True
        else:
            is_destination_empty = (
                (destination_amount / destination_capacity) <= 0.15
            ) or (destination_capacity - destination_amount >= quantity_requested)

        if destination_capacity > 0:
            quantity_requested = min(
                quantity_requested, destination_capacity - destination_amount
            )

        if source_capacity == 0:
            is_source_enough = True
        elif source_amount >= source_capacity * 0.85:
            is_source_enough = True
        else:
            is_source_enough = source_amount >= quantity_requested

        print(
            f"{job.name}: {source_amount}/{source_capacity} {destination_amount}/{destination_capacity}"
        )

        return is_destination_empty and is_source_enough


@final
class Ticket(models.Model):
    class Infringement(models.TextChoices):
        GRIEFING = "griefing", "Griefing"
        TROLLING = "trolling", "Trolling"
        NUISANCE = "nuisance", "Public Nuisance"
        CLUTERRING = "cluterring", "Cluterring"
        MISDEMEANOR = "misdemeanor", "Misdemeanor"
        OTHER = "other", "Other"

    character = models.ForeignKey(
        Character,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
    )
    player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    infringement = models.CharField(max_length=200, choices=Infringement)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(editable=False, auto_now_add=True)
    issued_by = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets_issued",
    )

    @classmethod
    def get_social_score_deduction(self, infringement):
        match infringement:
            case self.Infringement.GRIEFING:
                social_score_deduction = 20
            case self.Infringement.TROLLING:
                social_score_deduction = 15
            case self.Infringement.NUISANCE:
                social_score_deduction = 8
            case self.Infringement.CLUTERRING:
                social_score_deduction = 5
            case self.Infringement.MISDEMEANOR:
                social_score_deduction = 2
            case self.Infringement.OTHER:
                social_score_deduction = 1
            case _:
                social_score_deduction = 3
        return social_score_deduction


@final
class ServerStatus(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    num_players = models.PositiveIntegerField()
    fps = models.PositiveIntegerField()
    used_memory = models.PositiveBigIntegerField()


@final
class VehicleDecal(models.Model):
    name = models.CharField(max_length=128)
    player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, blank=True, related_name="decals"
    )
    config = models.JSONField(null=True, blank=True)
    hash = models.CharField(max_length=200, unique=True)
    vehicle_key = models.CharField(max_length=100, null=True)
    private = models.BooleanField(default=True)
    price = models.PositiveIntegerField(default=0)

    @staticmethod
    def calculate_hash(decal_config):
        hashes = DeepHash(decal_config)
        return hashes[decal_config]

    @override
    def __str__(self):
        return f"{self.name} - {self.hash[:8]}"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["hash", "player"], name="unique_player_decal_hash"
            ),
            models.UniqueConstraint(
                fields=["name", "player"], name="unique_player_decal_name"
            ),
        ]


@final
class PlayerShift(models.Model):
    player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, blank=True, related_name="shifts"
    )
    start_time_utc = models.TimeField(
        help_text="The start time of the user's shift in UTC."
    )
    end_time_utc = models.TimeField(
        help_text="The end time of the user's shift in UTC."
    )

    user_timezone = models.CharField(
        max_length=100,
        default="UTC",
        help_text="The user's local timezone (e.g., 'America/New_York').",
    )


@final
class RescueRequest(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="rescue_requests"
    )
    responders = models.ManyToManyField(Player, related_name="rescue_responses")
    discord_message_id = models.CharField(
        max_length=32, null=True, blank=True, unique=True
    )
    message = models.TextField(blank=True)
    location = models.PointField(srid=0, dim=3, null=True, blank=True)


@final
class CharacterVehicle(models.Model):
    character = models.ForeignKey(
        Character,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_vehicles",
    )
    vehicle_id = models.PositiveIntegerField(db_index=True)
    alias = models.CharField(max_length=32, null=True, blank=True)
    company_guid = models.CharField(max_length=32, null=True, blank=True)
    spawn_on_restart = models.BooleanField(default=False)
    rental = models.BooleanField(default=False)
    for_sale = models.BooleanField(default=False)
    config = models.JSONField()

    @override
    def __str__(self):
        if vehicle_name := self.config.get("VehicleName"):
            return f"{self.id} {vehicle_name}"
        return f"{self.id}"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["character", "vehicle_id"],
                name="unique_character_vehicle_id",
                condition=Q(character__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["company_guid", "vehicle_id"],
                name="unique_company_vehicle_id",
                condition=Q(character__isnull=True),
            ),
        ]


class WorldText(models.Model):
    """
    Represents a 3D text object to be spawned in the game world.
    """

    content = models.CharField(
        max_length=255, help_text="The text to display (e.g., PANZER)"
    )

    # Location Coordinates
    location_x = models.FloatField(help_text="World X Coordinate")
    location_y = models.FloatField(help_text="World Y Coordinate")
    location_z = models.FloatField(help_text="World Z Coordinate")

    # Orientation and Scale
    yaw = models.FloatField(default=0.0, help_text="Rotation in degrees (0=X+, 90=Y+)")
    scale = models.FloatField(default=5.0, help_text="Uniform scale factor")
    separation = models.FloatField(
        default=30.0, help_text="Distance between letters relative to scale"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.content} at ({self.location_x}, {self.location_y})"

    def generate_asset_data(self):
        """
        Calculates the individual character positions based on the script logic.
        Returns a list of dictionaries ready for spawning.
        """
        output_objects = []

        # Calculate the actual world-space distance between letters
        world_spacing = self.separation * self.scale

        # Calculate the midpoint index to center the text
        text_string = self.content
        num_chars = len(text_string)
        midpoint_index = (num_chars - 1) / 2.0

        # Convert Yaw to Radians
        yaw_rad = math.radians(self.yaw)

        # Calculate Direction Vector
        dir_x = math.cos(yaw_rad)
        dir_y = math.sin(yaw_rad)

        for i, char in enumerate(text_string):
            if char == " ":
                continue

            # Calculate distance of this specific character from the center point
            dist_from_center = (i - midpoint_index) * world_spacing

            # Project distance onto the direction vector
            x_offset = dist_from_center * dir_x
            y_offset = dist_from_center * dir_y

            current_location = {
                "Z": self.location_z,
                "X": self.location_x + x_offset,
                "Y": self.location_y + y_offset,
            }

            char_upper = char.upper()
            # Dynamic asset path construction
            asset_path = f"/Game/Models/PolygonIcons/Meshes/SM_Icon_Text_{char_upper}.SM_Icon_Text_{char_upper}"

            obj = {
                "AssetPath": asset_path,
                "decal": {"DecalLayers": {}},
                "Location": current_location,
                "scale": {"X": self.scale, "Z": self.scale, "Y": self.scale},
                "Rotation": {"Roll": 0, "Pitch": 0, "Yaw": self.yaw},
            }

            output_objects.append(obj)

        return output_objects


class WorldObject(models.Model):
    """
    Represents a 3D text object to be spawned in the game world.
    """

    asset_path = models.CharField(max_length=511, help_text="The asset path")

    # Location Coordinates
    location_x = models.FloatField(help_text="World X Coordinate")
    location_y = models.FloatField(help_text="World Y Coordinate")
    location_z = models.FloatField(help_text="World Z Coordinate")

    # Orientation and Scale
    yaw = models.FloatField(default=0.0, help_text="Rotation in degrees (0=X+, 90=Y+)")
    scale = models.FloatField(default=1.0, help_text="Uniform scale factor")

    def generate_asset_data(self):
        current_location = {
            "Z": self.location_z,
            "X": self.location_x,
            "Y": self.location_y,
        }
        return {
            "AssetPath": self.asset_path,
            "decal": {"DecalLayers": {}},
            "Location": current_location,
            "scale": {"X": self.scale, "Z": self.scale, "Y": self.scale},
            "Rotation": {"Roll": 0, "Pitch": 0, "Yaw": self.yaw},
        }


@final
class SubsidyArea(models.Model):
    name = models.CharField(max_length=200)
    polygon = models.PolygonField(srid=3857, dim=2)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


@final
class ShortcutZone(models.Model):
    name = models.CharField(max_length=200)
    polygon = models.PolygonField(srid=3857, dim=2)
    active = models.BooleanField(default=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


@final
class ArrestZone(models.Model):
    name = models.CharField(max_length=200)
    polygon = models.PolygonField(srid=3857, dim=2)
    active = models.BooleanField(default=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


@final
class SubsidyRule(models.Model):
    class RewardType(models.TextChoices):
        PERCENTAGE = "PERCENTAGE", _("Percentage")
        FLAT = "FLAT", _("Flat Amount")

    name = models.CharField(max_length=200)
    active = models.BooleanField(default=True)
    priority = models.IntegerField(
        default=0, help_text="Higher number = evaluated first"
    )

    # Conditions
    cargos = models.ManyToManyField(
        "Cargo", blank=True, help_text="If empty, applies to ALL cargos"
    )
    source_areas = models.ManyToManyField(
        SubsidyArea, related_name="source_rules", blank=True
    )
    destination_areas = models.ManyToManyField(
        SubsidyArea, related_name="destination_rules", blank=True
    )
    source_delivery_points = models.ManyToManyField(
        "DeliveryPoint", related_name="source_subsidy_rules", blank=True
    )
    destination_delivery_points = models.ManyToManyField(
        "DeliveryPoint", related_name="destination_subsidy_rules", blank=True
    )
    requires_on_time = models.BooleanField(default=False)

    # Rewards
    reward_type = models.CharField(max_length=20, choices=RewardType)
    reward_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Percentage (e.g. 3.0 for 300%) or Flat Amount",
    )
    scales_with_damage = models.BooleanField(
        default=False, help_text="If true, multiplies reward by health %"
    )

    # Ministry Budget Tracking
    allocation = models.DecimalField(
        max_digits=16,
        decimal_places=2,
        default=0,
        help_text="Ministry allocated budget for this rule",
    )
    spent = models.DecimalField(
        max_digits=16,
        decimal_places=2,
        default=0,
        help_text="Amount spent from the allocation",
    )

    if TYPE_CHECKING:
        reward_percentage: int

    def __str__(self):
        return f"{self.name} ({self.priority})"


@final
class JobPostingConfig(models.Model):
    """Server-wide job posting configuration. Singleton (pk=1 always)."""

    # Adaptive multiplier params
    target_success_rate = models.FloatField(
        default=0.50,
        help_text="Target job completion rate (0.0-1.0). Jobs scale up above this, down below.",
    )
    min_multiplier = models.FloatField(
        default=0.5,
        help_text="Minimum adaptive multiplier (scales down job count when success rate is low)",
    )
    max_multiplier = models.FloatField(
        default=1.5,
        help_text="Maximum adaptive multiplier (scales up job count when success rate is high)",
    )
    # Base job formula
    players_per_job = models.IntegerField(
        default=10,
        help_text="Legacy — unused since log2 curve replaced linear formula",
        validators=[MinValueValidator(1)],
    )
    min_base_jobs = models.IntegerField(
        default=1,
        help_text="Base offset for log2 job count curve (added to log2(1 + players))",
    )
    # Global posting probability multiplier
    posting_rate_multiplier = models.FloatField(
        default=1.0,
        help_text="Global multiplier on posting chance (0.5 = half rate, 2.0 = double rate)",
    )
    # Treasury-driven equilibrium params
    treasury_equilibrium = models.PositiveBigIntegerField(
        default=100_000_000,
        help_text="Treasury balance at which spending is 'normal' (multiplier = 1.0)",
    )
    treasury_sensitivity = models.FloatField(
        default=0.5,
        help_text="How aggressively spending changes with treasury balance (higher = steeper curve)",
    )
    max_posts_per_tick = models.PositiveIntegerField(
        default=3,
        help_text="Maximum number of new jobs to post per cron tick (rate limit)",
    )

    class Meta:
        verbose_name = "Job Posting Configuration"
        verbose_name_plural = "Job Posting Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass  # Prevent deletion of singleton

    @classmethod
    async def aget_config(cls) -> "JobPostingConfig":
        config, _ = await cls.objects.aget_or_create(pk=1)
        return config

    def __str__(self):
        return "Job Posting Configuration"


@final
class MinistryDashboard(models.Model):
    """
    Dummy model to expose the Ministry Dashboard in the Django Admin.
    """

    class Meta:
        managed = False
        verbose_name_plural = "Ministry Dashboard"


@final
class CharacterLocationStats(models.Model):
    character = models.OneToOneField(
        Character, on_delete=models.CASCADE, related_name="location_stats"
    )
    # Top vehicle by sample count
    favourite_vehicle = models.CharField(
        max_length=100, null=True, blank=True, choices=VehicleKey
    )
    # Full breakdown: {"vehicle_key": count, ...}
    vehicle_stats = models.JSONField(default=dict)
    total_location_records = models.PositiveIntegerField(default=0)
    # Timestamp of the latest CharacterLocation row that was processed
    last_computed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Character Location Stats"
        verbose_name_plural = "Character Location Stats"

    def __str__(self):
        label = VehicleKey(self.favourite_vehicle).label if self.favourite_vehicle else "None"
        return f"{self.character} — fav: {label}"


# ── Supply Chain Events ──────────────────────────────────────────────


class SupplyChainEventQuerySet(models.QuerySet):
    def filter_active(self):
        now = timezone.now()
        return self.filter(
            start_at__lte=now,
            end_at__gte=now,
            rewards_distributed=False,
        )

    def filter_active_or_future(self):
        now = timezone.now()
        return self.filter(end_at__gte=now, rewards_distributed=False)

    def filter_ended_not_distributed(self):
        now = timezone.now()
        return self.filter(end_at__lt=now, rewards_distributed=False)


@final
class SupplyChainEventManager(
    models.Manager.from_queryset(SupplyChainEventQuerySet)  # pyrefly: ignore [invalid-inheritance]
):  # type: ignore[misc]
    pass


@final
class SupplyChainEvent(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    reward_per_item = models.PositiveBigIntegerField(
        help_text="Reward per unit of primary objective delivered"
    )
    rewards_distributed = models.BooleanField(default=False)
    discord_message_id = models.PositiveBigIntegerField(null=True, blank=True)

    if TYPE_CHECKING:
        objectives: models.Manager["SupplyChainObjective"]

    objects: ClassVar[SupplyChainEventManager] = SupplyChainEventManager()

    @property
    def is_active(self):
        now = timezone.now()
        return self.start_at <= now <= self.end_at and not self.rewards_distributed

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(start_at__lt=F("end_at")),
                name="supply_chain_event_start_before_end",
            )
        ]

    def __str__(self):
        return self.name


@final
class SupplyChainObjective(models.Model):
    event = models.ForeignKey(
        SupplyChainEvent, on_delete=models.CASCADE, related_name="objectives"
    )
    cargos = models.ManyToManyField(
        Cargo, related_name="supply_chain_objectives", blank=True
    )
    destination_points = models.ManyToManyField(
        DeliveryPoint, related_name="supply_chain_objectives_in", blank=True
    )
    source_points = models.ManyToManyField(
        DeliveryPoint, related_name="supply_chain_objectives_out", blank=True
    )
    ceiling = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max rewardable quantity. Null = uncapped.",
    )
    quantity_fulfilled = models.PositiveIntegerField(default=0)
    reward_weight = models.PositiveIntegerField(
        default=10, help_text="Relative weight for reward pool share (e.g. 40 for 40%)"
    )
    is_primary = models.BooleanField(
        default=False, help_text="Primary objectives define the main event goal"
    )


    if TYPE_CHECKING:
        contributions: models.Manager["SupplyChainContribution"]

    def __str__(self):
        cargo_names = ", ".join(c.label for c in self.cargos.all()[:3])
        return f"{self.event.name} — {cargo_names or 'any cargo'}"


@final
class SupplyChainContribution(models.Model):
    objective = models.ForeignKey(
        SupplyChainObjective, on_delete=models.CASCADE, related_name="contributions"
    )
    character = models.ForeignKey(
        Character, on_delete=models.SET_NULL, null=True, related_name="supply_chain_contributions"
    )
    cargo_key = models.CharField(max_length=200, db_index=True, choices=CargoKey)
    quantity = models.PositiveIntegerField()
    timestamp = models.DateTimeField()
    delivery = models.ForeignKey(
        Delivery,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supply_chain_contributions",
    )


class SupplyChainEventTemplate(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    reward_per_item = models.PositiveBigIntegerField(default=10_000)
    duration_hours = models.FloatField(default=24.0)
    enabled = models.BooleanField(default=True)

    if TYPE_CHECKING:
        objectives: models.Manager["SupplyChainObjectiveTemplate"]

    def __str__(self):
        return self.name


@final
class SupplyChainObjectiveTemplate(models.Model):
    template = models.ForeignKey(
        SupplyChainEventTemplate, on_delete=models.CASCADE, related_name="objectives"
    )
    cargos = models.ManyToManyField(
        Cargo, related_name="sc_event_template_objectives", blank=True
    )
    destination_points = models.ManyToManyField(
        DeliveryPoint, related_name="sc_objective_templates_in", blank=True
    )
    source_points = models.ManyToManyField(
        DeliveryPoint, related_name="sc_objective_templates_out", blank=True
    )
    ceiling = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max rewardable quantity. Null = uncapped.",
    )
    reward_weight = models.PositiveIntegerField(
        default=10, help_text="Relative weight for reward pool share (e.g. 40 for 40%)"
    )
    is_primary = models.BooleanField(
        default=False, help_text="Primary objectives define the main event goal"
    )

    def __str__(self):
        cargo_names = ", ".join(c.label for c in self.cargos.all()[:3])
        return f"{self.template.name} — {cargo_names or 'any cargo'}"


@final
class Voucher(models.Model):
    """A claimable reward voucher with a unique code.

    Vouchers can be created with or without a player. When player is null,
    anyone with the code can claim it. The code is used in-game via
    /claim_voucher <code>.
    """

    code = models.CharField(max_length=10, unique=True, db_index=True)
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, null=True, blank=True, related_name="vouchers"
    )
    amount = models.PositiveIntegerField()
    reason = models.CharField(max_length=200)
    claimed_by = models.ForeignKey(
        Character,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="claimed_vouchers",
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    if TYPE_CHECKING:
        objects: ClassVar[models.Manager["Voucher"]]

    @staticmethod
    def generate_code(prefix: str = "V") -> str:
        """Generate a unique voucher code like 'V-A3F8K2'."""
        import secrets
        import string

        chars = string.ascii_uppercase + string.digits
        suffix = "".join(secrets.choice(chars) for _ in range(6))
        return f"{prefix}-{suffix}"

    @property
    def is_claimed(self):
        return self.claimed_at is not None

    @override
    def __str__(self):
        status = "claimed" if self.is_claimed else "unclaimed"
        return f"Voucher {self.code} - {self.amount:,} ({status})"


@final
class NewsItem(models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Leave blank to auto-expire 7 days after creation.",
    )

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    async def aget_active(cls, limit=4):
        """Return the latest unexpired news items.

        Items with an explicit expires_at are filtered by that date.
        Items without expires_at default to 7 days after created_at.
        """
        now = timezone.now()
        return [
            item
            async for item in cls.objects.filter(
                Q(expires_at__gt=now)
                | Q(expires_at__isnull=True, created_at__gt=now - timedelta(days=7))
            )[:limit]
        ]

    @override
    def __str__(self):
        return self.title

