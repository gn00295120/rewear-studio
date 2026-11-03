"""Evidence and fail-closed verdicts for synthetic garment display images.

The models describe observations, never authenticity or hidden construction.
Acceptance is computed from every explicit check, not a model's overall score.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from .reconstruction_sources import ReferenceManifest, SourceID


Text = Annotated[str, Field(min_length=1, max_length=600)]
RegionName = Annotated[str, Field(min_length=1, max_length=100)]
RegionBox = Annotated[
    list[Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]],
    Field(min_length=4, max_length=4),
]
SourceView = Literal["front", "back", "side", "unclear"]
FrontDisplayState = Literal["open", "partly_open", "closed", "overlapping", "not_applicable", "unknown"]
Verdict = Literal["accepted", "needs_review", "rejected"]
CheckStatus = Literal["pass", "fail", "uncertain"]
Feature = Literal[
    "colour",
    "pattern",
    "panel_geometry",
    "silhouette",
    "collar",
    "sleeves",
    "hem",
    "closures",
    "pockets",
    "graphics",
    "texture",
    "other",
]
CheckName = Literal[
    "viewpoint",
    "visible_details",
    "color_pattern",
    "cut_structure",
    "single_garment",
    "no_person_or_extras",
    "complete_outline",
]
CHECK_NAMES = (
    "viewpoint",
    "visible_details",
    "color_pattern",
    "cut_structure",
    "single_garment",
    "no_person_or_extras",
    "complete_outline",
)
PREVIEW_CHECK_NAMES = (
    "category",
    "coarse_color",
    "single_garment",
    "no_person_or_extras",
    "complete_outline",
)
PREVIEW_NOTICE = "AI 預覽・含推測；只確認完整展示的基本適用性，不代表已確認為同一件衣物。"
MAX_DETAIL_REGIONS = 5


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class VisibleFeature(StrictModel):
    feature: Feature
    detail: Text


class UncertainArea(StrictModel):
    area: Annotated[str, Field(min_length=1, max_length=100)]
    detail: Text
    critical: bool
    # Missing legacy scope stays unknown. It must never be inferred from prose.
    scope: Literal["displayed", "unshown", "unclear"] | None = None
    scope_evidence: Text | None = None
    unshown_view: Literal["front", "back"] | None = None

    @model_validator(mode="after")
    def consistent_scope(self):
        if self.scope is None:
            if self.scope_evidence is not None or self.unshown_view is not None:
                raise ValueError("Legacy uncertainty cannot claim partial scope evidence")
        elif self.scope_evidence is None:
            raise ValueError("Scoped uncertainty requires image evidence")
        elif (self.scope == "unshown") != (self.unshown_view is not None):
            raise ValueError("Only unshown uncertainty must identify its opposite face")
        return self


def _validate_box(box: list[float] | None) -> None:
    if box is not None and (box[2] - box[0] < 0.01 or box[3] - box[1] < 0.01):
        raise ValueError("Detail region must have a nonempty normalized box")


class FrontStateObservation(StrictModel):
    state: FrontDisplayState
    evidence: Text
    box: RegionBox | None

    @model_validator(mode="after")
    def located_observation(self):
        _validate_box(self.box)
        if self.state not in ("unknown", "not_applicable") and self.box is None:
            raise ValueError("A definite front display state requires a located image observation")
        return self


class PrimaryFrontState(FrontStateObservation):
    source_image: Literal["context", "cutout"]


class DetailRegion(StrictModel):
    name: RegionName
    feature: Feature
    source_image: SourceID
    box: RegionBox
    support: Literal["clear", "ambiguous"]
    detail: Text

    @model_validator(mode="after")
    def valid_box(self):
        _validate_box(self.box)
        return self


class SourceEvidencePlan(StrictModel):
    target: Literal["one_garment", "multiple_garments", "no_garment", "uncertain"]
    source_view: SourceView
    view_evidence: Text
    garment_type: Annotated[str, Field(min_length=1, max_length=200)] | None
    visibility: Literal["complete", "partial", "fragment", "unclear"]
    visible_features: list[VisibleFeature] = Field(max_length=12)
    uncertain_areas: list[UncertainArea] = Field(max_length=10)
    # Legacy plans stay readable; every new source API answer must supply this.
    # This is presentation in the primary photo, not inferred garment design.
    primary_front_state: PrimaryFrontState | None = None
    # Stored pre-region plans remain readable. New vision responses must supply
    # this field explicitly; PLAN_SCHEMA below makes it required for the API.
    detail_regions: list[DetailRegion] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def unique_features(self):
        if len({entry.feature for entry in self.visible_features}) != len(self.visible_features):
            raise ValueError("Each visible feature must have one evidence entry")
        if len({entry.name for entry in self.detail_regions}) != len(self.detail_regions):
            raise ValueError("Each detail region must have a unique name")
        opposite = {"front": "back", "back": "front"}.get(self.source_view)
        if any(
            area.scope == "unshown" and (opposite is None or area.unshown_view != opposite)
            for area in self.uncertain_areas
        ):
            raise ValueError("Unshown uncertainty must belong to the opposite reconstruction face")
        return self


class FidelityCheck(StrictModel):
    status: CheckStatus
    reason: Text


class FidelityChecks(StrictModel):
    viewpoint: FidelityCheck
    visible_details: FidelityCheck
    color_pattern: FidelityCheck
    cut_structure: FidelityCheck
    single_garment: FidelityCheck
    no_person_or_extras: FidelityCheck
    complete_outline: FidelityCheck


class PreviewObservations(StrictModel):
    category: FidelityCheck
    coarse_color: FidelityCheck


class PreviewChecks(PreviewObservations):
    single_garment: FidelityCheck
    no_person_or_extras: FidelityCheck
    complete_outline: FidelityCheck


class InferredArea(StrictModel):
    area: RegionName
    detail: Text


class PreviewAssessment(StrictModel):
    version: Literal[1]
    status: Literal["ready", "needs_review", "rejected", "unverified"]
    source_view: SourceView
    display_view: Literal["front", "back"]
    view_inferred: bool
    checks: PreviewChecks | None
    inferred_areas: list[InferredArea] = Field(max_length=14)
    # Seven checks, twelve features, five regions and the independent front
    # comparison may each contribute a distinct retained failure reason.
    mismatch_warnings: list[Text] = Field(max_length=25)
    notice: Literal[PREVIEW_NOTICE]

    @model_validator(mode="after")
    def consistent_preview(self):
        expected_view = "back" if self.source_view == "back" else "front"
        if self.display_view != expected_view or self.view_inferred != (
            self.source_view not in ("front", "back")
        ):
            raise ValueError("Preview display view must retain or explicitly infer the source face")
        states = [getattr(self.checks, name).status for name in PREVIEW_CHECK_NAMES] if self.checks else []
        expected_status = (
            "unverified"
            if not states
            else "rejected"
            if "fail" in states
            else "needs_review"
            if "uncertain" in states
            else "ready"
        )
        if self.status != expected_status:
            raise ValueError("Preview readiness must follow all five explicit image checks")
        return self


class FeatureCheck(StrictModel):
    feature: Feature
    status: CheckStatus
    reason: Text


class Correction(StrictModel):
    check: CheckName
    instruction: Text


class CandidateRegion(StrictModel):
    name: RegionName
    box: RegionBox | None

    @model_validator(mode="after")
    def valid_box(self):
        _validate_box(self.box)
        return self


class RegionalCheck(StrictModel):
    name: RegionName
    feature: Feature
    source_observation: Text
    candidate_observation: Text
    status: CheckStatus
    reason: Text
    correction: Text | None


class RegionalAnswer(StrictModel):
    regional_checks: list[RegionalCheck] = Field(max_length=MAX_DETAIL_REGIONS)


class SourceDetailAnswer(StrictModel):
    detail_regions: list[DetailRegion] = Field(max_length=3)
    visible_features: list[VisibleFeature] = Field(default_factory=list, max_length=4)
    # Source-only review may discover details omitted by the initial plan.
    # Keep their own regions instead of hiding several locations in one key.
    additional_regions: list[DetailRegion] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def unique_features(self):
        if len({entry.feature for entry in self.visible_features}) != len(self.visible_features):
            raise ValueError("Source detail constraints must have unique feature keys")
        regions = [*self.detail_regions, *self.additional_regions]
        if len({entry.name for entry in regions}) != len(regions):
            raise ValueError("Source detail regions must retain distinct evidence names")
        return self


PlanningField = Literal[
    "response",
    "target",
    "source_view",
    "primary_front_state",
    "state",
    "evidence",
    "view_evidence",
    "garment_type",
    "visibility",
    "visible_features",
    "uncertain_areas",
    "detail_regions",
    "additional_regions",
    "feature",
    "detail",
    "area",
    "critical",
    "scope",
    "scope_evidence",
    "unshown_view",
    "name",
    "source_image",
    "box",
    "support",
]


class PlanningIssue(StrictModel):
    code: Literal[
        "json",
        "schema",
        "missing",
        "invalid_type",
        "length",
        "enum",
        "duplicate",
        "box",
        "coverage",
        "reference",
        "incomplete",
        "provider_error",
        "timeout",
    ]
    path: list[PlanningField | Annotated[int, Field(ge=0, le=100)]] = Field(min_length=1, max_length=6)


class PlanningAttempt(StrictModel):
    stage: Literal["source_plan", "source_refinement"]
    attempt: int = Field(ge=1, le=2)
    status: Literal["valid", "invalid", "error"]
    issues: list[PlanningIssue] = Field(max_length=8)
    source_plan: SourceEvidencePlan | None = None
    source_details: SourceDetailAnswer | None = None

    @model_validator(mode="after")
    def consistent_response(self):
        if self.stage == "source_plan" and self.source_details is not None:
            raise ValueError("Plan diagnostic cannot contain refinement output")
        if self.stage == "source_refinement" and self.source_plan is not None:
            raise ValueError("Refinement diagnostic cannot contain initial plan output")
        parsed = self.source_plan if self.stage == "source_plan" else self.source_details
        if self.status == "valid" and (parsed is None or self.issues):
            raise ValueError("Valid planning diagnostic requires parsed evidence and no errors")
        if self.status != "valid" and not self.issues:
            raise ValueError("Failed planning diagnostic requires bounded structural issues")
        return self


def validate_planning_diagnostics(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError("At most four planning diagnostic entries are allowed")
    parsed = [PlanningAttempt.model_validate(entry) for entry in value]
    stages = {"source_plan": [], "source_refinement": []}
    refining = False
    for entry in parsed:
        if entry.stage == "source_refinement":
            refining = True
        elif refining:
            raise ValueError("Initial planning cannot follow refinement")
        previous = stages[entry.stage]
        if entry.attempt != len(previous) + 1 or (previous and previous[-1].status != "invalid"):
            raise ValueError("Planning retries must follow one invalid structured response")
        previous.append(entry)
    return [entry.model_dump() for entry in parsed]


class ReconstructionPlan(SourceEvidencePlan):
    # Initial planning stays limited to three. Independent original-pixel
    # review can add two missed details without discarding those obligations.
    detail_regions: list[DetailRegion] = Field(default_factory=list, max_length=MAX_DETAIL_REGIONS)
    # These are adapter-owned provenance, never fields requested from a model.
    # Old stored reports remain readable with no reconstruction of lost history.
    initial_source_plan: SourceEvidencePlan | None = None
    original_ambiguous_regions: list[DetailRegion] = Field(default_factory=list, max_length=3)
    planning_attempts: list[PlanningAttempt] = Field(default_factory=list, max_length=4)
    reference_manifest: ReferenceManifest | None = None

    @model_validator(mode="after")
    def retain_source_obligations(self):
        if self.initial_source_plan is None:
            if self.original_ambiguous_regions:
                raise ValueError("Original uncertainty requires original source evidence")
        elif self.original_ambiguous_regions != [
            region for region in self.initial_source_plan.detail_regions if region.support == "ambiguous"
        ]:
            raise ValueError("Source refinement cannot erase original ambiguous obligations")
        if (
            self.initial_source_plan is not None
            and self.uncertain_areas != self.initial_source_plan.uncertain_areas
        ):
            raise ValueError("Source refinement cannot change original uncertain areas or scope")
        if (
            self.initial_source_plan is not None
            and self.primary_front_state != self.initial_source_plan.primary_front_state
        ):
            raise ValueError("Source refinement cannot change the primary front display state")
        validate_planning_diagnostics([entry.model_dump() for entry in self.planning_attempts])
        regions = [
            *self.detail_regions,
            *self.original_ambiguous_regions,
            *(self.initial_source_plan.detail_regions if self.initial_source_plan else []),
        ]
        available = {"cutout", "context"}
        if self.reference_manifest is not None:
            available = {source.id for source in self.reference_manifest.sources}
            if any(source.reference_sha256 is None for source in self.reference_manifest.sources):
                raise ValueError("A source plan must bind every transmitted reference hash")
        if any(region.source_image not in available for region in regions):
            raise ValueError("Every original detail requires its declared reference manifest source")
        if self.primary_front_state is not None and (
            self.primary_front_state.source_image not in available
            or (
                self.reference_manifest is not None
                and self.primary_front_state.source_image != self.reference_manifest.primary_source
            )
        ):
            raise ValueError("Front display state must come from the primary reference")
        return self


class FidelityAnswer(StrictModel):
    observed_view: SourceView
    candidate_front_state: FrontStateObservation | None = None
    checks: FidelityChecks
    feature_checks: list[FeatureCheck] = Field(max_length=12)
    corrections: list[Correction] = Field(max_length=7)
    region_boxes: list[CandidateRegion] = Field(default_factory=list, max_length=MAX_DETAIL_REGIONS)
    # Legacy comparison data does not establish coarse preview suitability.
    preview_observations: PreviewObservations | None = None


class ReconstructionAttempt(StrictModel):
    number: int = Field(ge=1, le=2)
    observed_view: SourceView
    candidate_front_state: FrontStateObservation | None = None
    checks: FidelityChecks
    feature_checks: list[FeatureCheck] = Field(max_length=12)
    status: Verdict
    issues: list[Text] = Field(max_length=14)
    corrective_feedback: list[Text] = Field(max_length=7)
    regional_checks: list[RegionalCheck] = Field(default_factory=list, max_length=MAX_DETAIL_REGIONS)
    preview: PreviewAssessment | None = None


class ReconstructionRepairTargets(StrictModel):
    """Internal edit scope, containing identifiers rather than model commands."""

    checks: list[CheckName] = Field(default_factory=list, max_length=7)
    features: list[Feature] = Field(default_factory=list, max_length=12)
    regions: list[Annotated[int, Field(ge=0, lt=MAX_DETAIL_REGIONS)]] = Field(
        default_factory=list, max_length=MAX_DETAIL_REGIONS
    )


def preview_plan_issues(plan: ReconstructionPlan) -> list[str]:
    """Preview completion may infer hidden construction, never the target itself."""
    reasons = []
    if plan.target != "one_garment":
        reasons.append("原始參考無法確認為一件明確的目標衣物。")
    if plan.visibility not in ("complete", "partial"):
        reasons.append("目標只剩片段或無法辨認，沒有足夠依據製作完整預覽。")
    if not plan.garment_type or not plan.visible_features:
        reasons.append("衣物種類或可見特徵不足，不能可靠製作預覽。")
    return reasons


def plan_issues(plan: ReconstructionPlan) -> list[str]:
    reasons = []
    if plan.target != "one_garment":
        reasons.append("原始參考無法確認為一件明確的目標衣物。")
    if plan.source_view not in ("front", "back"):
        reasons.append("原圖僅有側面或視角不清楚，無法確定應保留的衣物展示面。")
    if plan.visibility not in ("complete", "partial"):
        reasons.append("目標只剩片段或結構無法辨認，需要更完整的原始照片。")
    if not plan.garment_type or not plan.visible_features:
        reasons.append("原始照片沒有足夠可見款式依據，不能可靠重建。")
    reasons.extend(area.detail for area in plan.uncertain_areas if area.critical)
    return reasons[:14]


def _verdict(checks: FidelityChecks) -> Verdict:
    states = [getattr(checks, name).status for name in CHECK_NAMES]
    if "fail" in states:
        return "rejected"
    return "needs_review" if "uncertain" in states else "accepted"


class ReconstructionReport(StrictModel):
    version: Literal[1]
    status: Verdict
    source_view: SourceView
    candidate_front_state: FrontStateObservation | None = None
    plan: ReconstructionPlan
    checks: FidelityChecks | None
    feature_checks: list[FeatureCheck] = Field(max_length=12)
    regional_checks: list[RegionalCheck] = Field(default_factory=list, max_length=MAX_DETAIL_REGIONS)
    issues: list[Text] = Field(max_length=14)
    attempts: list[ReconstructionAttempt] = Field(max_length=2)
    generated: bool
    retried: bool
    # Legacy reports used the final chronological attempt. New reports name the
    # selected image explicitly without rewriting the history of failed edits.
    selected_attempt: int | None = Field(default=None, ge=1, le=2)
    revision_error: Text | None = None
    preview: PreviewAssessment | None = None

    @model_validator(mode="after")
    def consistent_evidence(self, info: ValidationInfo):
        if self.source_view != self.plan.source_view or self.retried != (len(self.attempts) == 2):
            raise ValueError("Reconstruction report disagrees with its evidence")
        if self.revision_error is not None and (not self.generated or len(self.attempts) != 1):
            raise ValueError("Failed revision must retain one previously generated candidate")
        if self.generated:
            if not self.attempts or self.checks is None or preview_plan_issues(self.plan):
                raise ValueError("Generated result requires a supported plan and comparison")
            for number, attempt in enumerate(self.attempts, 1):
                if attempt.number != number or attempt.status != _verdict(attempt.checks):
                    raise ValueError("Attempt verdict does not follow its checks")
                _validate_feature_coverage(self.plan, attempt.feature_checks)
                _validate_region_coverage(self.plan, attempt.regional_checks)
                _validate_view_and_features(
                    self.plan,
                    attempt,
                    allow_legacy_scope_upgrade=bool((info.context or {}).get("legacy_scope_upgrade")),
                )
                _validate_preview(self.plan, attempt)
            if self.selected_attempt is not None and self.selected_attempt > len(self.attempts):
                raise ValueError("Selected attempt must exist in the chronological history")
            selected = self.attempts[(self.selected_attempt or len(self.attempts)) - 1]
            if (
                self.status != selected.status
                or self.checks != selected.checks
                or self.feature_checks != selected.feature_checks
                or self.regional_checks != selected.regional_checks
                or self.issues != selected.issues
                or self.preview != selected.preview
                or self.candidate_front_state != selected.candidate_front_state
            ):
                raise ValueError("Final report must describe the selected compared candidate")
        elif (
            self.attempts
            or self.checks is not None
            or self.feature_checks
            or self.regional_checks
            or self.selected_attempt is not None
            or self.preview is not None
            or self.candidate_front_state is not None
            or self.status == "accepted"
        ):
            raise ValueError("Ungenerated source cannot claim a passed image comparison")
        if self.status == "accepted" and (
            not self.generated or self.source_view not in ("front", "back") or plan_issues(self.plan)
        ):
            raise ValueError("Acceptance requires supported original evidence")
        return self


def _validate_feature_coverage(plan: ReconstructionPlan, features: list[FeatureCheck]) -> None:
    expected = {entry.feature for entry in plan.visible_features}
    if (
        len({entry.feature for entry in features}) != len(features)
        or {entry.feature for entry in features} != expected
    ):
        raise ValueError("Comparison must check every visible source feature exactly once")


def _validate_region_coverage(plan: ReconstructionPlan, checks: list[RegionalCheck]) -> None:
    expected = {entry.name: entry.feature for entry in plan.detail_regions}
    if (
        len({entry.name for entry in checks}) != len(checks)
        or {entry.name: entry.feature for entry in checks} != expected
    ):
        raise ValueError("Comparison must check every source detail region exactly once")


def _region_dimension(feature: Feature) -> str:
    return "color_pattern" if feature in ("colour", "pattern", "graphics", "texture") else "cut_structure"


_SEVERITY = {"pass": 0, "uncertain": 1, "fail": 2}


def front_state_comparison(
    plan: ReconstructionPlan, candidate: FrontStateObservation | None
) -> FidelityCheck | None:
    """Compare separately observed primary/candidate states, never their prose.

    Legacy absence adds no retroactive claim. Not-applicable covers a pullover
    without a front opening, or a front that is not displayed in a back view.
    Missing/unknown observations for an applicable primary remain unverified.
    """
    source = plan.primary_front_state
    if source is None:
        return (
            None
            if candidate is None
            else FidelityCheck(
                status="uncertain", reason="缺少主照前襟狀態依據，不能確認候選觀察與原圖一致。"
            )
        )
    if source.state == "not_applicable":
        return None
    if source.state == "unknown":
        return FidelityCheck(status="uncertain", reason="主照前襟展示狀態無法確認，不能宣稱候選已保留。")
    if candidate is None or candidate.state in ("unknown", "not_applicable"):
        return FidelityCheck(status="uncertain", reason="候選缺少可確認的前襟展示觀察，不能判定與主照一致。")
    if candidate.state == source.state:
        return FidelityCheck(status="pass", reason="候選的獨立前襟展示觀察與主照狀態一致。")
    labels = {"open": "敞開分離", "partly_open": "部分敞開", "closed": "靠攏閉合", "overlapping": "前片交疊"}
    return FidelityCheck(
        status="fail",
        reason=f"主照前襟為「{labels[source.state]}」，候選為「{labels[candidate.state]}」，未保留主照展示狀態。",
    )


def _apply_front_state(plan: ReconstructionPlan, response: FidelityAnswer) -> None:
    compared = front_state_comparison(plan, response.candidate_front_state)
    if compared is None or compared.status == "pass":
        return
    for name in ("visible_details", "cut_structure"):
        if _SEVERITY[compared.status] > _SEVERITY[getattr(response.checks, name).status]:
            setattr(response.checks, name, compared.model_copy(deep=True))
    for feature in response.feature_checks:
        if feature.feature == "closures" and _SEVERITY[compared.status] > _SEVERITY[feature.status]:
            feature.status, feature.reason = compared.status, compared.reason


def _source_uncertainties(plan: ReconstructionPlan) -> list[UncertainArea]:
    original = plan.initial_source_plan if plan.initial_source_plan is not None else plan
    return original.uncertain_areas


def _unresolved_display_scope(plan: ReconstructionPlan) -> list[UncertainArea]:
    opposite = {"front": "back", "back": "front"}.get(plan.source_view)
    return [
        area
        for area in _source_uncertainties(plan)
        if not (
            not area.critical
            and area.scope == "unshown"
            and area.scope_evidence
            and opposite is not None
            and area.unshown_view == opposite
        )
    ]


def preview_display_view(plan: ReconstructionPlan) -> Literal["front", "back"]:
    return "back" if plan.source_view == "back" else "front"


def _preview_inferences(plan: ReconstructionPlan) -> list[InferredArea]:
    areas = [InferredArea(area=area.area, detail=area.detail) for area in _unresolved_display_scope(plan)]
    original = plan.initial_source_plan if plan.initial_source_plan is not None else plan
    areas.extend(
        InferredArea(area=region.name, detail=region.detail)
        for region in original.detail_regions
        if region.support == "ambiguous"
    )
    if plan.source_view not in ("front", "back"):
        areas.append(InferredArea(area="展示面", detail="原圖未完整確立正面或背面，預覽採推測的正面展示。"))
    return areas


def _preview_warnings(plan: ReconstructionPlan, attempt: ReconstructionAttempt) -> list[str]:
    warnings = [
        getattr(attempt.checks, name).reason
        for name in CHECK_NAMES
        if getattr(attempt.checks, name).status == "fail"
    ]
    warnings.extend(entry.reason for entry in attempt.feature_checks if entry.status == "fail")
    warnings.extend(entry.reason for entry in attempt.regional_checks if entry.status == "fail")
    front = front_state_comparison(plan, attempt.candidate_front_state)
    if front is not None and front.status == "fail":
        warnings.append(front.reason)
    return list(dict.fromkeys(warnings))


def _preview_assessment(
    plan: ReconstructionPlan,
    attempt: ReconstructionAttempt,
    observed: PreviewObservations | None,
) -> PreviewAssessment:
    checks = None
    status = "unverified"
    if observed is not None:
        checks = PreviewChecks(
            **observed.model_dump(),
            **{name: getattr(attempt.checks, name).model_dump() for name in PREVIEW_CHECK_NAMES[2:]},
        )
        states = [getattr(checks, name).status for name in PREVIEW_CHECK_NAMES]
        status = "rejected" if "fail" in states else "needs_review" if "uncertain" in states else "ready"
    return PreviewAssessment(
        version=1,
        status=status,
        source_view=plan.source_view,
        display_view=preview_display_view(plan),
        view_inferred=plan.source_view not in ("front", "back"),
        checks=checks,
        inferred_areas=_preview_inferences(plan),
        mismatch_warnings=_preview_warnings(plan, attempt),
        notice=PREVIEW_NOTICE,
    )


def _validate_preview(plan: ReconstructionPlan, attempt: ReconstructionAttempt) -> None:
    preview = attempt.preview
    if preview is None:
        return
    if (
        preview.source_view != plan.source_view
        or preview.inferred_areas != _preview_inferences(plan)
        or preview.mismatch_warnings != _preview_warnings(plan, attempt)
    ):
        raise ValueError("Preview must retain source uncertainty and every known fidelity mismatch")
    if preview.checks is not None and any(
        getattr(preview.checks, name) != getattr(attempt.checks, name) for name in PREVIEW_CHECK_NAMES[2:]
    ):
        raise ValueError("Preview and fidelity must share the same complete single-garment observations")


def _apply_uncertainty_scope(plan: ReconstructionPlan, checks: FidelityChecks) -> bool:
    unresolved = _unresolved_display_scope(plan)
    if not unresolved or checks.visible_details.status != "pass":
        return False
    checks.visible_details = FidelityCheck(
        status="uncertain",
        reason=(
            "原圖未知細節尚未證明位於不展示的另一面，完整展示仍需人工核對："
            + "；".join(area.area + "：" + area.detail for area in unresolved)
        )[:600],
    )
    return True


def _validate_view_and_features(
    plan: ReconstructionPlan,
    attempt: ReconstructionAttempt,
    *,
    allow_legacy_scope_upgrade: bool = False,
) -> None:
    front = front_state_comparison(plan, attempt.candidate_front_state)
    if front is not None and front.status != "pass":
        if any(
            _SEVERITY[getattr(attempt.checks, name).status] < _SEVERITY[front.status]
            for name in ("visible_details", "cut_structure")
        ) or any(
            feature.feature == "closures" and _SEVERITY[feature.status] < _SEVERITY[front.status]
            for feature in attempt.feature_checks
        ):
            raise ValueError(
                "Primary front display mismatch or uncertainty cannot be hidden by passed checks"
            )
    if attempt.observed_view != plan.source_view and attempt.checks.viewpoint.status == "pass":
        raise ValueError("Changed or uncertain viewpoint cannot pass")
    if (
        any(feature.status != "pass" for feature in attempt.feature_checks)
        and attempt.checks.visible_details.status == "pass"
    ):
        raise ValueError("Failed or uncertain source details cannot pass")
    source_regions = {entry.name: entry for entry in plan.detail_regions}
    for check in attempt.regional_checks:
        if source_regions[check.name].support == "ambiguous" and check.status != "uncertain":
            raise ValueError("Ambiguous source region cannot prove either agreement or mismatch")
        for name in ("visible_details", _region_dimension(check.feature)):
            if _SEVERITY[getattr(attempt.checks, name).status] < _SEVERITY[check.status]:
                raise ValueError("Regional uncertainty or mismatch cannot be hidden by an overall check")
    for obligation in plan.original_ambiguous_regions:
        for name in ("visible_details", _region_dimension(obligation.feature)):
            if getattr(attempt.checks, name).status == "pass":
                raise ValueError("Refined visible evidence cannot certify unresolved original scope")
    legacy_scope = any(area.scope is None for area in _source_uncertainties(plan))
    if (
        not (allow_legacy_scope_upgrade and legacy_scope)
        and _unresolved_display_scope(plan)
        and attempt.checks.visible_details.status == "pass"
    ):
        raise ValueError("Displayed or unscoped source uncertainty cannot certify full fidelity")


def assess_fidelity(plan: dict, answer: dict, number: int, regional_checks: list[dict] | None = None) -> dict:
    """Cross-check independently observed view and every planned detail."""
    source = ReconstructionPlan.model_validate(plan)
    response = FidelityAnswer.model_validate(answer)
    _validate_feature_coverage(source, response.feature_checks)
    regions = [RegionalCheck.model_validate(entry) for entry in (regional_checks or [])]
    _validate_region_coverage(source, regions)
    if response.preview_observations is not None:
        for observation, dimension in (("category", "visible_details"), ("coarse_color", "color_pattern")):
            observed = getattr(response.preview_observations, observation)
            current = getattr(response.checks, dimension)
            if _SEVERITY[observed.status] > _SEVERITY[current.status]:
                setattr(response.checks, dimension, observed.model_copy(deep=True))
    if source.source_view not in ("front", "back"):
        if response.checks.viewpoint.status == "pass":
            response.checks.viewpoint = FidelityCheck(
                status="uncertain", reason="原圖未完整確立正面或背面，推測的展示面不能視為同一視角已核對。"
            )
    elif response.observed_view != source.source_view:
        response.checks.viewpoint = FidelityCheck(
            status="uncertain" if response.observed_view == "unclear" else "fail",
            reason=f"原圖為 {source.source_view} 視角，生成圖判讀為 {response.observed_view}，未保留同一展示面。",
        )
    feature_problems = [entry for entry in response.feature_checks if entry.status != "pass"]
    if feature_problems and response.checks.visible_details.status != "fail":
        response.checks.visible_details = FidelityCheck(
            status="fail" if any(entry.status == "fail" for entry in feature_problems) else "uncertain",
            reason="；".join(entry.reason for entry in feature_problems)[:600],
        )
    source_regions = {entry.name: entry for entry in source.detail_regions}
    for check in regions:
        original = source_regions[check.name]
        if original.support == "ambiguous":
            # Source-only analysis happened before the candidate existed. Its
            # ambiguity cannot disappear because a plausible rendering suggests
            # what might have been under hands, folds, hair or another garment.
            check.status = "uncertain"
            check.correction = None
            check.reason = ("原圖此處依據不完整，需人工核對：" + original.detail)[:600]
        if check.status != "fail":
            check.correction = None
        if check.status != "pass":
            for name in ("visible_details", _region_dimension(check.feature)):
                current = getattr(response.checks, name)
                if _SEVERITY[check.status] >= _SEVERITY[current.status]:
                    setattr(response.checks, name, FidelityCheck(status=check.status, reason=check.reason))
    for obligation in source.original_ambiguous_regions:
        # Keep clear refined-region observations useful. The original unknown
        # is a separate obligation, and can never overwrite a definite failure.
        for name in ("visible_details", _region_dimension(obligation.feature)):
            if getattr(response.checks, name).status == "pass":
                setattr(
                    response.checks,
                    name,
                    FidelityCheck(
                        status="uncertain",
                        reason=(
                            "原圖仍有未解的完整性依據，局部清楚不能代表整個範圍已確認："
                            + obligation.name
                            + "；"
                            + obligation.detail
                        )[:600],
                    ),
                )
    _apply_uncertainty_scope(source, response.checks)
    _apply_front_state(source, response)
    status = _verdict(response.checks)
    issues = [
        getattr(response.checks, name).reason
        for name in CHECK_NAMES
        if getattr(response.checks, name).status != "pass"
    ]
    # A correction is actionable only for an observed failure, never an unknown
    # area or a request to invent unseen details. The prompt remains authoritative.
    # Paired original/candidate crops are the most specific correction evidence.
    # Do not let a long list of global advice crowd out a tiny visible mismatch.
    corrections = [check.correction for check in regions if check.status == "fail" and check.correction]
    corrections.extend(
        entry.instruction
        for entry in response.corrections
        if getattr(response.checks, entry.check).status == "fail"
    )
    attempt = ReconstructionAttempt(
        number=number,
        observed_view=response.observed_view,
        candidate_front_state=response.candidate_front_state,
        checks=response.checks,
        feature_checks=response.feature_checks,
        status=status,
        issues=issues,
        corrective_feedback=list(dict.fromkeys(corrections))[:7],
        regional_checks=regions,
    )
    attempt.preview = _preview_assessment(source, attempt, response.preview_observations)
    return attempt.model_dump()


def unverified_attempt(plan: dict, number: int, reason: str) -> dict:
    source = ReconstructionPlan.model_validate(plan)
    attempt = ReconstructionAttempt(
        number=number,
        observed_view="unclear",
        status="needs_review",
        issues=[reason] * len(CHECK_NAMES),
        checks=FidelityChecks.model_validate(
            {name: {"status": "uncertain", "reason": reason} for name in CHECK_NAMES}
        ),
        feature_checks=[
            FeatureCheck(feature=entry.feature, status="uncertain", reason=reason)
            for entry in source.visible_features
        ],
        regional_checks=[
            RegionalCheck(
                name=entry.name,
                feature=entry.feature,
                source_observation=entry.detail,
                candidate_observation="候選圖片核對未完成，沒有可確認的區域觀察。",
                status="uncertain",
                reason=reason,
                correction=None,
            )
            for entry in source.detail_regions
        ],
        corrective_feedback=[],
    )
    attempt.preview = _preview_assessment(source, attempt, None)
    return attempt.model_dump()


def reconstruction_repair_targets(plan: dict, attempt: dict) -> dict:
    """Select only failed, source-supported targets; never execute audit prose.

    Candidate-only shape/extras checks remain actionable without a textual
    command. Design repairs need a named failed feature or a clear original
    region. An ambiguous source region cannot authorize repairing its hidden
    design, even if a later global answer claims a mismatch.
    """
    source = ReconstructionPlan.model_validate(plan)
    observed = ReconstructionAttempt.model_validate(attempt)
    _validate_feature_coverage(source, observed.feature_checks)
    _validate_region_coverage(source, observed.regional_checks)
    targets = ReconstructionRepairTargets()
    if source.target != "one_garment" or observed.status != "rejected":
        return targets.model_dump()

    for name in ("single_garment", "no_person_or_extras", "complete_outline"):
        if getattr(observed.checks, name).status == "fail":
            targets.checks.append(name)
    if source.source_view in ("front", "back") and observed.checks.viewpoint.status == "fail":
        targets.checks.append("viewpoint")

    regional = {entry.name: entry for entry in observed.regional_checks}
    targets.regions = [
        index
        for index, region in enumerate(source.detail_regions)
        if region.support == "clear" and regional[region.name].status == "fail"
    ]
    regional_features = {source.detail_regions[index].feature for index in targets.regions}
    ambiguous_features = {
        region.feature
        for region in [*source.detail_regions, *source.original_ambiguous_regions]
        if region.support == "ambiguous"
    }
    targets.features = [
        entry.feature
        for entry in observed.feature_checks
        if entry.status == "fail"
        and entry.feature not in ambiguous_features
        and entry.feature not in regional_features
    ]
    front = front_state_comparison(source, observed.candidate_front_state)
    if front is not None and front.status == "fail" and "closures" not in targets.features:
        # An observed primary display state is independent of hidden button or
        # fastener construction. Only that state is authorized for this repair.
        targets.features.append("closures")
    design_features = regional_features | set(targets.features)
    dimensions = {_region_dimension(feature) for feature in design_features}
    if design_features:
        dimensions.add("visible_details")
    targets.checks.extend(
        name for name in CHECK_NAMES if name in dimensions and getattr(observed.checks, name).status == "fail"
    )
    return ReconstructionRepairTargets.model_validate(targets.model_dump()).model_dump()


def select_attempt(attempts: list[dict], *, plan: dict | None = None) -> int | None:
    """Keep the earlier image unless a later audit improves without regression.

    Every check remains binding. A new unknown is not allowed to replace a
    previously confirmed match merely because another defect disappeared.
    This selection never changes any individual verdict or uncertainty.
    """
    if not attempts:
        return None
    source = ReconstructionPlan.model_validate(plan) if plan is not None else None

    def states(attempt: ReconstructionAttempt) -> dict[str, int]:
        front = front_state_comparison(source, attempt.candidate_front_state) if source is not None else None
        return {
            **({"primary_front_state": _SEVERITY[front.status] if front else 0} if source else {}),
            **{f"check:{key}": _SEVERITY[getattr(attempt.checks, key).status] for key in CHECK_NAMES},
            **{f"feature:{entry.feature}": _SEVERITY[entry.status] for entry in attempt.feature_checks},
            **{f"region:{entry.name}": _SEVERITY[entry.status] for entry in attempt.regional_checks},
            **{
                f"preview:{name}": _SEVERITY[getattr(attempt.preview.checks, name).status]
                if attempt.preview and attempt.preview.checks
                else _SEVERITY["uncertain"]
                for name in PREVIEW_CHECK_NAMES
            },
        }

    parsed = [ReconstructionAttempt.model_validate(value) for value in attempts]
    selected = parsed[0]
    for candidate in parsed[1:]:
        before, after = states(selected), states(candidate)
        if before.keys() != after.keys():
            raise ValueError("Candidate comparison coverage must remain identical")
        if all(after[key] <= before[key] for key in before) and any(
            after[key] < before[key] for key in before
        ):
            selected = candidate
    return selected.number


def build_report(
    plan: dict,
    attempts: list[dict],
    *,
    selected_attempt: int | None = None,
    revision_error: str | None = None,
) -> dict:
    source = ReconstructionPlan.model_validate(plan)
    selected_number = (
        selected_attempt if selected_attempt is not None else (len(attempts) if attempts else None)
    )
    if selected_number is not None and not 1 <= selected_number <= len(attempts):
        raise ValueError("Selected attempt must exist")
    selected = attempts[selected_number - 1] if selected_number is not None else None
    return validate_report(
        {
            "version": 1,
            "status": selected["status"]
            if selected
            else ("rejected" if source.target in ("multiple_garments", "no_garment") else "needs_review"),
            "source_view": source.source_view,
            "candidate_front_state": selected.get("candidate_front_state") if selected else None,
            "plan": source.model_dump(),
            "checks": selected["checks"] if selected else None,
            "feature_checks": selected["feature_checks"] if selected else [],
            "regional_checks": selected.get("regional_checks", []) if selected else [],
            "issues": selected["issues"] if selected else plan_issues(source),
            "attempts": attempts,
            "generated": bool(attempts),
            "retried": len(attempts) == 2,
            "selected_attempt": selected_number,
            "revision_error": revision_error,
            "preview": selected.get("preview") if selected else None,
        }
    )


def validate_report(value) -> dict:
    # First enforce every existing consistency rule. Only a missing legacy
    # scope may defer the new guard; malformed or forged reports stay invalid.
    report = ReconstructionReport.model_validate(value, context={"legacy_scope_upgrade": True})
    if any(area.scope is None for area in _source_uncertainties(report.plan)):
        for attempt in report.attempts:
            if _apply_uncertainty_scope(report.plan, attempt.checks):
                attempt.status = _verdict(attempt.checks)
                attempt.issues = [
                    getattr(attempt.checks, name).reason
                    for name in CHECK_NAMES
                    if getattr(attempt.checks, name).status != "pass"
                ]
        if report.attempts:
            selected = report.attempts[(report.selected_attempt or len(report.attempts)) - 1]
            report.status = selected.status
            report.checks = selected.checks.model_copy(deep=True)
            report.issues = list(selected.issues)
    return ReconstructionReport.model_validate(report.model_dump()).model_dump()


def is_accepted(value) -> bool:
    try:
        return validate_report(value)["status"] == "accepted"
    except (ValueError, TypeError):
        return False


def is_preview_ready(value) -> bool:
    try:
        report = validate_report(value)
        return bool(report["generated"] and report["preview"] and report["preview"]["status"] == "ready")
    except (ValueError, TypeError):
        return False


PLAN_SCHEMA = SourceEvidencePlan.model_json_schema()
FIDELITY_SCHEMA = FidelityAnswer.model_json_schema()
PLAN_SCHEMA["required"].append("detail_regions")
PLAN_SCHEMA["required"].append("primary_front_state")
PLAN_SCHEMA["properties"]["primary_front_state"] = {"$ref": "#/$defs/PrimaryFrontState"}
_uncertainty_schema = PLAN_SCHEMA["$defs"]["UncertainArea"]
_uncertainty_schema["required"].extend(["scope", "scope_evidence", "unshown_view"])
_uncertainty_schema["properties"]["scope"] = {
    "type": "string",
    "enum": ["displayed", "unshown", "unclear"],
}
_uncertainty_schema["properties"]["scope_evidence"] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 600,
}
FIDELITY_SCHEMA["required"].append("region_boxes")
FIDELITY_SCHEMA["required"].append("preview_observations")
FIDELITY_SCHEMA["required"].append("candidate_front_state")
FIDELITY_SCHEMA["properties"]["candidate_front_state"] = {"$ref": "#/$defs/FrontStateObservation"}
# New audits must actually observe these two criteria; null remains readable
# only in pre-preview stored/model fixtures, never an API success claim.
FIDELITY_SCHEMA["properties"]["preview_observations"] = {"$ref": "#/$defs/PreviewObservations"}
REGIONAL_SCHEMA = RegionalAnswer.model_json_schema()
SOURCE_DETAIL_SCHEMA = SourceDetailAnswer.model_json_schema()
SOURCE_DETAIL_SCHEMA["required"].append("visible_features")
SOURCE_DETAIL_SCHEMA["required"].append("additional_regions")
