"""Configuration Items derived from infrastructure-as-code files on disk.

Why files and not cloud APIs: parsing the IaC in the repository needs no credentials, no
network and no SDK (both ``oci`` and ``boto3`` are third-party), works air-gapped and on
Windows, and inventories the *intended* estate the contingency plan is written to protect.
A cloud API would inventory what exists right now, which is drift detection - a different
job with a different consumer, and one that would drag two providers' auth flows and
pagination into a document generator. Cloud discovery is a non-goal for 1.0.

Three inputs, all standard library:

``terraform``
    A directory of ``.tf`` files, read by the block scanner below.
``terraform_json``
    The output of ``terraform show -json``. Structure confirmed against HashiCorp's "JSON
    Output Format" page (``https://developer.hashicorp.com/terraform/internals/json-format``,
    fetched 2026-09-02): ``values.root_module.resources[]`` with ``address``, ``mode``,
    ``type``, ``name``, optional ``index``, ``provider_name``, ``schema_version`` and
    ``values``; ``child_modules[]`` repeat that shape with their own ``address``. Values are
    fully resolved, which is why this input is preferred when the user has it.
``cloudformation_json``
    A CloudFormation template in JSON. YAML is refused: the standard library has no YAML
    parser and guessing at one is worse than saying so.

**HCL scanning without a parser.** The standard library has no HCL parser and the plan
forbids adding one, so this is a *block scanner*, not a grammar. It masks comments and string
interiors so brace counting is safe, then recognises exactly four top-level constructs -
``resource``, ``module``, ``locals`` and ``provider``. Inside a resource it records top-level
``key = literal`` pairs, nested block names, ``count``/``for_each`` and ``provider = alias``.
``for_each`` over a literal ``locals`` map is expanded into one CI per key; any other
``count``/``for_each`` yields one CI whose ``_multiplicity`` attribute holds the expression.
Anything it cannot classify is reported with ``file:line`` under "unrecognised" or "warnings".
It never raises on odd input - a contingency plan generator that dies on an unusual ``.tf``
file is useless at the moment it is needed.

Known limits, stated so nobody assumes otherwise: expressions are not evaluated, so
``var.shape`` stays the string ``var.shape``; ``dynamic`` blocks and ``for`` expressions
produce fewer CIs than they will resources; Terraform's own module ``source`` resolution is
not followed beyond recording it.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class CI:
    """One Configuration Item: a resource the contingency plan has to account for."""
    ci_id: str
    name: str
    address: str
    resource_type: str
    category: str
    cloud: str
    region: str | None = None
    site: str | None = None
    attributes: dict = field(default_factory=dict)
    source: str = ""
    depends_on: list[str] = field(default_factory=list)
    replication: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "CI":
        known = {f: data.get(f) for f in CI.__dataclass_fields__}
        known["attributes"] = known.get("attributes") or {}
        known["depends_on"] = known.get("depends_on") or []
        return CI(**known)


@dataclass
class Inventory:
    """What one import produced, including everything it could not classify."""
    cis: list[CI] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unrecognised: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------- resource type -> category

#: Resource-type prefix -> CI category. Longest prefix wins, so ``oci_core_volume_group``
#: beats ``oci_core_``. Covers every ``oci_*`` type present in the reference plan the user
#: authorised (``oci-itscp/terraform``) plus the common AWS equivalents. Anything not listed
#: is ``other`` and is *reported*, never guessed at.
CATEGORIES: dict[str, str] = {
    # --- OCI
    "oci_core_instance": "compute",
    "oci_core_instance_pool": "compute",
    "oci_core_instance_configuration": "compute",
    "oci_core_volume_group": "storage",
    "oci_core_volume_attachment": "storage",
    "oci_core_volume": "storage",
    "oci_core_boot_volume": "storage",
    "oci_core_vcn": "network",
    "oci_core_subnet": "network",
    "oci_core_route_table": "network",
    "oci_core_internet_gateway": "network",
    "oci_core_nat_gateway": "network",
    "oci_core_service_gateway": "network",
    "oci_core_drg": "network",
    "oci_core_drg_attachment": "network",
    "oci_core_remote_peering_connection": "network",
    "oci_core_network_security_group_security_rule": "security",
    "oci_core_network_security_group": "security",
    "oci_core_security_list": "security",
    "oci_objectstorage_replication_policy": "backup",
    "oci_objectstorage_bucket": "storage",
    "oci_file_storage_replication_target": "backup",
    "oci_file_storage_replication": "backup",
    "oci_file_storage_file_system": "storage",
    "oci_file_storage_mount_target": "storage",
    "oci_file_storage_export": "storage",
    "oci_load_balancer_load_balancer": "load_balancer",
    "oci_load_balancer_listener": "load_balancer",
    "oci_load_balancer_backend_set": "load_balancer",
    "oci_load_balancer_backend": "load_balancer",
    "oci_network_load_balancer": "load_balancer",
    "oci_waf_web_app_firewall_policy": "security",
    "oci_waf_web_app_firewall": "security",
    "oci_dns_steering_policy_attachment": "dns",
    "oci_dns_steering_policy": "dns",
    "oci_dns_zone": "dns",
    "oci_dns_view": "dns",
    "oci_dns_rrset": "dns",
    "oci_database_data_guard_association": "dr_orchestration",
    "oci_database_cloud_exadata_infrastructure": "database",
    "oci_database_cloud_vm_cluster": "database",
    "oci_database_db_system": "database",
    "oci_database_autonomous_database": "database",
    "oci_mysql_mysql_db_system": "database",
    "oci_disaster_recovery_dr_protection_group": "dr_orchestration",
    "oci_disaster_recovery_dr_plan_execution": "dr_orchestration",
    "oci_disaster_recovery_dr_plan": "dr_orchestration",
    "oci_recovery_protected_database": "backup",
    "oci_recovery_protection_policy": "backup",
    "oci_recovery_recovery_service_subnet": "backup",
    "oci_monitoring_alarm": "monitoring",
    "oci_health_checks_http_monitor": "monitoring",
    "oci_ons_notification_topic": "monitoring",
    "oci_ons_subscription": "monitoring",
    "oci_resource_scheduler_schedule": "dr_orchestration",
    "oci_identity_": "identity",
    "oci_kms_": "security",
    "oci_vault_": "security",
    # --- AWS (Terraform)
    "aws_instance": "compute",
    "aws_autoscaling_group": "compute",
    "aws_launch_template": "compute",
    "aws_ecs_service": "compute",
    "aws_lambda_function": "compute",
    "aws_db_instance": "database",
    "aws_rds_cluster_instance": "database",
    "aws_rds_cluster": "database",
    "aws_dynamodb_table": "database",
    "aws_elasticache_replication_group": "database",
    "aws_s3_bucket_replication_configuration": "backup",
    "aws_s3_bucket": "storage",
    "aws_efs_replication_configuration": "backup",
    "aws_efs_file_system": "storage",
    "aws_ebs_snapshot": "backup",
    "aws_ebs_volume": "storage",
    "aws_fsx_": "storage",
    "aws_lb_target_group": "load_balancer",
    "aws_lb_listener": "load_balancer",
    "aws_lb": "load_balancer",
    "aws_elb": "load_balancer",
    "aws_route53_health_check": "dns",
    "aws_route53_": "dns",
    "aws_globalaccelerator_": "dns",
    "aws_vpc_peering_connection": "network",
    "aws_vpc": "network",
    "aws_subnet": "network",
    "aws_route_table": "network",
    "aws_internet_gateway": "network",
    "aws_nat_gateway": "network",
    "aws_security_group": "security",
    "aws_kms_key": "security",
    "aws_iam_": "identity",
    "aws_backup_": "backup",
    "aws_drs_": "dr_orchestration",
    "aws_cloudwatch_": "monitoring",
    "aws_sns_topic": "monitoring",
    # --- CloudFormation
    "AWS::EC2::Instance": "compute",
    "AWS::EC2::Volume": "storage",
    "AWS::EC2::VPC": "network",
    "AWS::EC2::Subnet": "network",
    "AWS::EC2::SecurityGroup": "security",
    "AWS::AutoScaling::AutoScalingGroup": "compute",
    "AWS::RDS::DBCluster": "database",
    "AWS::RDS::DBInstance": "database",
    "AWS::DynamoDB::Table": "database",
    "AWS::S3::Bucket": "storage",
    "AWS::EFS::FileSystem": "storage",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "load_balancer",
    "AWS::Route53::": "dns",
    "AWS::Backup::": "backup",
    "AWS::KMS::Key": "security",
    "AWS::IAM::": "identity",
    "AWS::CloudWatch::": "monitoring",
}

#: Resource types that *are* a DR mechanism, and the sentence recorded on ``CI.replication``.
DR_RESOURCE_TYPES: dict[str, str] = {
    "oci_database_data_guard_association": "Data Guard association",
    "oci_objectstorage_replication_policy": "Object Storage replication policy",
    "oci_file_storage_replication": "File Storage replication",
    "oci_file_storage_replication_target": "File Storage replication target",
    "oci_disaster_recovery_dr_protection_group": "Full Stack DR protection group",
    "oci_disaster_recovery_dr_plan": "Full Stack DR plan",
    "oci_recovery_protected_database": "Database Autonomous Recovery Service protection",
    "aws_s3_bucket_replication_configuration": "S3 bucket replication",
    "aws_efs_replication_configuration": "EFS replication",
    "aws_backup_plan": "AWS Backup plan",
    "aws_drs_replication_configuration_template": "Elastic Disaster Recovery replication",
}

#: Attribute or nested-block names that mark an otherwise ordinary resource as replicated.
DR_MARKERS: tuple[str, ...] = (
    "volume_group_replicas",
    "replicate_source_db",
    "source_db_cluster_identifier",
    "replication_configuration",
)

#: Attributes that name where replication goes, in order of preference.
_DESTINATION_KEYS = ("destination_region", "target_region", "availability_domain",
                     "destination_bucket")

#: Where the region can be read from directly, in order of preference.
_REGION_KEYS = ("region", "destination_region", "target_region")


def categorise(resource_type: str) -> str:
    """Longest matching prefix in :data:`CATEGORIES`, else ``other``."""
    best = ""
    for prefix in CATEGORIES:
        if resource_type.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return CATEGORIES[best] if best else "other"


def cloud_of(resource_type: str) -> str:
    if resource_type.startswith("oci_"):
        return "oci"
    if resource_type.startswith(("aws_", "AWS::")):
        return "aws"
    return "unknown"


# --------------------------------------------------------------------------- HCL scanning

_MASK = "\x00"
_RESOURCE = re.compile(r'resource\s+"([^"\n]+)"\s+"([^"\n]+)"\s*\{')
_MODULE = re.compile(r'module\s+"([^"\n]+)"\s*\{')
_PROVIDER = re.compile(r'provider\s+"([^"\n]+)"\s*\{')
_LOCALS = re.compile(r"locals\s*\{")
_ASSIGN = re.compile(r'^[ \t]*([A-Za-z_][A-Za-z0-9_-]*)[ \t]*=[ \t]*(.*)$')
_NESTED = re.compile(r'^[ \t]*([A-Za-z_][A-Za-z0-9_-]*)[ \t]*\{[ \t]*$', re.M)
_REFERENCE = re.compile(r'\b([a-z][a-z0-9_]*)\.([A-Za-z0-9_-]+)(?:\.[A-Za-z0-9_\[\]"]+)?')
_STRING_LITERAL = re.compile(r'^"([^"]*)"$')


def _mask(source: str) -> tuple[str, list[str]]:
    """Blank out comments and string interiors so brace counting is safe.

    Returns the masked text (same length as ``source``, newlines preserved so line numbers
    still work) and a list of warnings for constructs that never terminated.
    """
    out = list(source)
    warnings: list[str] = []
    index, length = 0, len(source)
    while index < length:
        char = source[index]
        if char == "#" or source.startswith("//", index):
            while index < length and source[index] != "\n":
                out[index] = " "
                index += 1
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = length if end == -1 else end + 2
            if end == length:
                warnings.append(f"unterminated block comment at offset {index}")
            for position in range(index, end):
                if source[position] != "\n":
                    out[position] = " "
            index = end
        elif char == '"':
            index += 1
            while index < length and source[index] != '"':
                if source[index] == "\\":
                    out[index] = _MASK
                    index += 1
                if index < length:
                    out[index] = _MASK if source[index] != "\n" else "\n"
                    index += 1
            index += 1
        elif source.startswith("<<", index):
            index = _mask_heredoc(source, out, index, warnings)
        else:
            index += 1
    return "".join(out), warnings


def _mask_heredoc(source: str, out: list[str], index: int, warnings: list[str]) -> int:
    """Mask a ``<<EOT`` / ``<<-EOT`` body. Returns the index just past it."""
    marker_match = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)").match(source, index)
    if not marker_match:
        return index + 2
    marker = marker_match.group(1)
    body_start = source.find("\n", marker_match.end())
    if body_start == -1:
        return len(source)
    end = re.compile(rf"^[ \t]*{re.escape(marker)}[ \t]*$", re.M).search(source, body_start + 1)
    stop = end.end() if end else len(source)
    if not end:
        warnings.append(f"unterminated heredoc <<{marker} at offset {index}")
    for position in range(body_start + 1, stop):
        if source[position] != "\n":
            out[position] = _MASK
    return stop


def _depths(masked: str) -> list[int]:
    """Brace depth *before* each character, so ``depths[pos] == 0`` means top level."""
    depths, depth = [], 0
    for char in masked:
        depths.append(depth)
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
    return depths


def _block_span(masked: str, open_brace: int) -> int:
    """Index just past the ``}`` matching the ``{`` at ``open_brace``, or end of text."""
    depth = 0
    for position in range(open_brace, len(masked)):
        if masked[position] == "{":
            depth += 1
        elif masked[position] == "}":
            depth -= 1
            if depth == 0:
                return position + 1
    return len(masked)


def _line_of(source: str, position: int) -> int:
    return source.count("\n", 0, position) + 1


def _literal(raw: str) -> object:
    """A literal HCL value, or the expression string when it is not one."""
    value = raw.strip().rstrip(",")
    match = _STRING_LITERAL.match(value)
    if match:
        return match.group(1)
    if value in ("true", "false"):
        return value == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


class _Block:
    """One top-level HCL block, already sliced out of its file.

    ``attributes`` holds top-level ``key = value`` pairs with literals unwrapped;
    ``quoted`` names the keys whose value was a quoted string, so reference extraction can
    skip them (``instance_type = "m6i.large"`` is not a reference to a resource called
    ``large``); ``nested`` maps each nested block's name to its own attributes, which is
    where a DR mechanism like ``volume_group_replicas`` keeps its destination.
    """

    def __init__(self, source: str, masked: str, start: int, end: int, line: int,
                 resource_type: str = "", resource_name: str = ""):
        self.text = source[start:end]
        self.masked = masked[start:end]
        self.line = line
        self.resource_type = resource_type
        self.resource_name = resource_name
        self.attributes: dict[str, object] = {}
        self.quoted: set[str] = set()
        self.nested: dict[str, dict] = {}
        self._read_body()

    def _read_body(self) -> None:
        """Read ``key = value`` pairs and nested blocks one level inside this block."""
        self.attributes, self.quoted = _read_assignments(self.text, self.masked, depth=1)
        depths = _depths(self.masked)
        for match in _NESTED.finditer(self.masked):
            if depths[match.start()] != 1:
                continue
            open_brace = self.masked.index("{", match.start())
            end = _block_span(self.masked, open_brace)
            inner, _ = _read_assignments(self.text[open_brace:end], self.masked[open_brace:end],
                                         depth=1)
            self.nested.setdefault(match.group(1), inner)


def _read_assignments(text: str, masked: str, depth: int) -> tuple[dict[str, object], set[str]]:
    """``key = value`` pairs at exactly ``depth`` braces inside ``masked``.

    Matching happens on the *original* text so quoted values survive; ``masked`` supplies the
    depth and proves the line is not inside a comment or a string.
    """
    depths = _depths(masked)
    attributes: dict[str, object] = {}
    quoted: set[str] = set()
    offset = 0
    for masked_line, line in zip(masked.split("\n"), text.split("\n")):
        if offset >= len(depths):                # unterminated block: nothing left to read
            break
        if depths[offset] == depth and _ASSIGN.match(masked_line):
            match = _ASSIGN.match(line)
            if match:
                raw = match.group(2).strip().rstrip(",")
                attributes[match.group(1)] = _literal(raw)
                if _STRING_LITERAL.match(raw):
                    quoted.add(match.group(1))
        offset += len(masked_line) + 1
    return attributes, quoted


def _top_level(source: str, masked: str, depths: list[int], pattern: re.Pattern):
    """Matches of ``pattern`` in the real source that sit at brace depth 0 and are code.

    The pattern has to run against ``source`` - the resource type and name live inside quotes,
    whose interiors ``masked`` has blanked out - so each hit is checked back against ``masked``
    to reject one that fell inside a comment, a string or a heredoc body.
    """
    for match in pattern.finditer(source):
        start = match.start()
        if depths[start] == 0 and masked[start] == source[start]:
            yield match


def _scan_file(path: Path, source: str) -> tuple[list[_Block], dict, dict, list[str], list[str]]:
    """Split one ``.tf`` file into resource blocks, provider aliases and ``locals`` maps."""
    masked, mask_warnings = _mask(source)
    warnings = [f"{path.name}: {w}" for w in mask_warnings]
    depths = _depths(masked)
    if masked.count("{") != masked.count("}"):
        warnings.append(f"{path.name}: unbalanced braces ({masked.count('{')} open, "
                        f"{masked.count('}')} close); resources after the imbalance may be missed")

    resources: list[_Block] = []
    aliases: dict[str, str] = {}
    local_maps: dict[str, list[str]] = {}
    modules: list[str] = []
    for match in _top_level(source, masked, depths, _RESOURCE):
        open_brace = masked.index("{", match.start())
        resources.append(_Block(source, masked, open_brace, _block_span(masked, open_brace),
                                _line_of(source, match.start()), match.group(1), match.group(2)))
    for match in _top_level(source, masked, depths, _PROVIDER):
        open_brace = masked.index("{", match.start())
        block = _Block(source, masked, open_brace, _block_span(masked, open_brace), 0)
        alias, region = block.attributes.get("alias"), block.attributes.get("region")
        if isinstance(alias, str) and isinstance(region, str) and "alias" in block.quoted \
                and "region" in block.quoted:
            aliases[alias] = region
    for match in _top_level(source, masked, depths, _LOCALS):
        open_brace = masked.index("{", match.start())
        local_maps.update(_locals_maps(source, masked, open_brace, _block_span(masked, open_brace)))
    for match in _top_level(source, masked, depths, _MODULE):
        open_brace = masked.index("{", match.start())
        block = _Block(source, masked, open_brace, _block_span(masked, open_brace), 0)
        module_source = block.attributes.get("source")
        if not (isinstance(module_source, str) and module_source.startswith((".", "/"))):
            modules.append(f'{match.group(1)} (source {module_source!r})')
    return resources, aliases, local_maps, warnings, modules


def _locals_maps(source: str, masked: str, open_brace: int, end: int) -> dict[str, list[str]]:
    """``locals { name = { "KEY" = {...} } }`` -> ``{"name": ["KEY", ...]}``.

    Only literal, quoted keys of an object one level inside the local are collected; that is
    the pattern a ``for_each`` can be expanded over without evaluating anything.
    """
    body_masked, body = masked[open_brace:end], source[open_brace:end]
    depths = _depths(body_masked)
    maps: dict[str, list[str]] = {}
    for match in re.finditer(r'^[ \t]*([A-Za-z_][A-Za-z0-9_-]*)[ \t]*=[ \t]*\{', body_masked, re.M):
        if depths[match.start()] != 1:
            continue
        inner_open = match.end() - 1
        inner_end = _block_span(body_masked, inner_open)
        inner_depths = _depths(body_masked[inner_open:inner_end])
        keys = [key.group(1)
                for key in re.finditer(r'"([^"\n]+)"[ \t]*=', body[inner_open:inner_end])
                if inner_depths[key.start()] == 1]
        if keys:
            maps[match.group(1)] = keys
    return maps


def _references(block: _Block) -> list[str]:
    """``depends_on`` entries plus literal ``type.name`` references in expression values.

    Values that were quoted strings are skipped: ``instance_type = "m6i.large"`` is not a
    reference to a resource named ``large``, and treating it as one would put invented
    dependencies into the plan.
    """
    found: list[str] = []
    for key, value in block.attributes.items():
        if key in block.quoted or key == "provider" or not isinstance(value, str):
            continue
        for match in _REFERENCE.finditer(value):
            kind, name = match.group(1), match.group(2)
            if kind in ("var", "local", "each", "data", "module", "count", "aws", "oci"):
                continue
            reference = f"{kind}.{name}"
            if reference not in found:
                found.append(reference)
    return found


def _replication_of(resource_type: str, attributes: dict, nested: dict[str, dict]) -> str | None:
    """The DR mechanism this resource carries, phrased for a runbook step, or ``None``.

    Two ways a resource can carry one: its *type* is a DR mechanism (a Data Guard association,
    an Object Storage replication policy), or it has an attribute or nested block that marks
    replication on an otherwise ordinary resource (a volume group with ``volume_group_replicas``,
    an RDS instance with ``replicate_source_db``). The destination is quoted only when the
    source actually states it.
    """
    destination = next((attributes[key] for key in _DESTINATION_KEYS
                        if isinstance(attributes.get(key), str)), None)
    if resource_type in DR_RESOURCE_TYPES:
        mechanism = DR_RESOURCE_TYPES[resource_type]
        return f"{mechanism} -> {destination}" if destination else mechanism
    for marker in DR_MARKERS:
        if marker in nested:
            inner = nested[marker]
            target = next((inner[key] for key in _DESTINATION_KEYS if isinstance(inner.get(key), str)),
                          None)
            return f"{marker} -> {target}" if target else marker
        if marker in attributes:
            return f"{marker} -> {attributes[marker]}" if attributes[marker] else marker
    return None


def scan_terraform_dir(root: Path) -> Inventory:
    """Every ``resource`` block under ``root``, as CIs. Never raises on bad input."""
    inventory = Inventory()
    files = sorted(p for p in root.rglob("*.tf") if p.is_file())
    if not files:
        inventory.warnings.append(f"no .tf files under {root}")
        return inventory

    aliases: dict[str, str] = {}
    local_maps: dict[str, list[str]] = {}
    per_file: list[tuple[Path, list[_Block]]] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:                                   # unreadable file, not a bug
            inventory.warnings.append(f"{path.name}: cannot read ({exc})")
            continue
        resources, file_aliases, file_locals, warnings, modules = _scan_file(path, source)
        aliases.update(file_aliases)
        local_maps.update(file_locals)
        inventory.warnings.extend(warnings)
        per_file.append((path, resources))
        for module in modules:
            inventory.warnings.append(
                f"{path.name}: module {module} is not a local path, so its resources are not in "
                f"this scan; run terraform show -json instead to include them")

    for path, resources in per_file:
        for block in resources:
            inventory.cis.extend(_block_to_cis(block, path, root, aliases, local_maps))
    for ci in inventory.cis:
        if ci.category == "other":
            inventory.unrecognised[ci.resource_type] = inventory.unrecognised.get(ci.resource_type, 0) + 1
    _assign_ids(inventory.cis)
    return inventory


def _block_to_cis(block: _Block, path: Path, root: Path, aliases: dict, local_maps: dict) -> list[CI]:
    """One resource block -> one CI, or one per key when ``for_each`` walks a literal map."""
    attributes = {key: value for key, value in block.attributes.items()
                  if key not in ("provider", "depends_on", "for_each", "count")}
    provider = block.attributes.get("provider")
    region = aliases.get(provider.split(".", 1)[1]) if isinstance(provider, str) and "." in provider \
        else None
    if region is None:
        region = next((attributes[key] for key in _REGION_KEYS
                       if isinstance(attributes.get(key), str) and key in block.quoted), None)
    base = dict(
        resource_type=block.resource_type,
        category=categorise(block.resource_type),
        cloud=cloud_of(block.resource_type),
        region=region,
        source=f"{path.relative_to(root).as_posix()}:{block.line}",
        depends_on=_dependencies(block),
        replication=_replication_of(block.resource_type, attributes, block.nested),
    )
    if block.nested:
        attributes = {**attributes, "_blocks": sorted(block.nested)}

    for_each = block.attributes.get("for_each")
    local_name = str(for_each)[len("local."):] if isinstance(for_each, str) \
        and for_each.startswith("local.") else ""
    keys = local_maps.get(local_name)
    if keys:
        return [CI(ci_id="", name=key, attributes=attributes,
                   address=f'{block.resource_type}.{block.resource_name}["{key}"]', **base)
                for key in keys]

    multiplicity = block.attributes.get("for_each", block.attributes.get("count"))
    if multiplicity is not None:
        attributes = {**attributes, "_multiplicity": str(multiplicity)}
    display = attributes.get("display_name") or attributes.get("name")
    name = display if isinstance(display, str) and "${" not in display else block.resource_name
    return [CI(ci_id="", name=name, attributes=attributes,
               address=f"{block.resource_type}.{block.resource_name}", **base)]


def _dependencies(block: _Block) -> list[str]:
    """Explicit ``depends_on`` plus literal references found in expression attribute values."""
    found = list(_references(block))
    depends = block.attributes.get("depends_on")
    if isinstance(depends, str):
        for match in _REFERENCE.finditer(depends):
            reference = f"{match.group(1)}.{match.group(2)}"
            if reference not in found:
                found.append(reference)
    return found


# ------------------------------------------------------------------- terraform show -json

def read_terraform_show_json(path: Path) -> Inventory:
    """Read ``terraform show -json`` output. Values here are already resolved."""
    inventory = Inventory()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        inventory.warnings.append(f"{path.name}: not readable as JSON ({exc})")
        return inventory
    root_module = (document.get("values") or {}).get("root_module") or {}
    _walk_modules(root_module, path.name, inventory)
    for ci in inventory.cis:
        if ci.category == "other":
            inventory.unrecognised[ci.resource_type] = inventory.unrecognised.get(ci.resource_type, 0) + 1
    _assign_ids(inventory.cis)
    return inventory


def _walk_modules(module: dict, source_name: str, inventory: Inventory) -> None:
    for resource in module.get("resources") or []:
        if resource.get("mode") not in (None, "managed"):
            continue
        resource_type = resource.get("type") or ""
        values = resource.get("values") or {}
        nested = {key: value[0] for key, value in values.items()
                  if isinstance(value, list) and value and isinstance(value[0], dict)}
        flat = {key: value for key, value in values.items() if not isinstance(value, (dict, list))}
        replication = _replication_of(resource_type, values, nested)
        inventory.cis.append(CI(
            ci_id="", name=values.get("display_name") or values.get("name") or resource.get("name") or "",
            address=resource.get("address") or f"{resource_type}.{resource.get('name')}",
            resource_type=resource_type, category=categorise(resource_type),
            cloud=cloud_of(resource_type),
            region=next((values.get(k) for k in _REGION_KEYS if isinstance(values.get(k), str)), None),
            attributes=flat, source=source_name, depends_on=list(resource.get("depends_on") or []),
            replication=replication))
    for child in module.get("child_modules") or []:
        _walk_modules(child, source_name, inventory)


# ----------------------------------------------------------------- CloudFormation (JSON)

def read_cloudformation_json(path: Path) -> Inventory:
    """Read a CloudFormation template in JSON. YAML templates are refused, not guessed at."""
    inventory = Inventory()
    if path.suffix.lower() in (".yaml", ".yml"):
        inventory.warnings.append(
            f"{path.name}: CloudFormation YAML is not supported - the standard library has no YAML "
            f"parser and this plugin takes no third-party dependencies. Convert the template to JSON "
            f"(`aws cloudformation package`/`cfn-flip`) or point at the Terraform sources instead.")
        return inventory
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        inventory.warnings.append(f"{path.name}: not readable as JSON ({exc})")
        return inventory
    for logical_id, resource in (document.get("Resources") or {}).items():
        resource_type = resource.get("Type") or ""
        properties = {key: value for key, value in (resource.get("Properties") or {}).items()
                      if not isinstance(value, (dict, list))}
        inventory.cis.append(CI(
            ci_id="", name=str(properties.get("Name") or logical_id), address=logical_id,
            resource_type=resource_type, category=categorise(resource_type),
            cloud=cloud_of(resource_type), attributes=properties, source=path.name,
            depends_on=_cfn_dependencies(resource),
            replication=_replication_of(resource_type, properties, {})))
    for ci in inventory.cis:
        if ci.category == "other":
            inventory.unrecognised[ci.resource_type] = inventory.unrecognised.get(ci.resource_type, 0) + 1
    _assign_ids(inventory.cis)
    return inventory


def _cfn_dependencies(resource: dict) -> list[str]:
    depends = resource.get("DependsOn") or []
    return [depends] if isinstance(depends, str) else list(depends)


# --------------------------------------------------------------------------- CI identity

def _assign_ids(cis: list[CI]) -> None:
    """``CI-0001`` upwards, ordered by address so a re-import of the same sources is stable."""
    for number, ci in enumerate(sorted(cis, key=lambda c: (c.source, c.address)), start=1):
        ci.ci_id = f"CI-{number:04d}"


def merge(existing: list[CI], imported: list[CI], source_prefix: str, replace: bool) -> list[CI]:
    """Add ``imported`` to ``existing``; with ``replace``, drop what came from the same source."""
    kept = [ci for ci in existing if not (replace and ci.source.startswith(source_prefix))]
    addresses = {ci.address for ci in kept}
    kept.extend(ci for ci in imported if ci.address not in addresses)
    _assign_ids(kept)
    return kept


def prefill_rows(cis: list[CI], query: str, columns: tuple[str, ...]) -> list[dict]:
    """CI-derived draft rows for a question whose ``prefill`` is ``query``.

    ``cis:*`` is every CI; ``cis:storage,database`` restricts to those categories; ``regions``
    yields one row per distinct region. A draft is offered to the user, never stored for them.
    """
    if query == "regions":
        return _region_rows(cis, columns)
    if not query.startswith("cis:"):
        return []
    wanted = query[4:]
    chosen = cis if wanted == "*" else [ci for ci in cis if ci.category in wanted.split(",")]
    return [_ci_row(ci, columns, position) for position, ci in enumerate(chosen, start=1)]


def _ci_row(ci: CI, columns: tuple[str, ...], position: int) -> dict:
    """One CI as a row of ``columns``. Only cells the CI actually knows are filled."""
    values = {
        "System Resource/Component": f"{ci.ci_id} {ci.name}".strip(),
        "Platform/OS/Version (as applicable)": ci.resource_type,
        "Description": f"{ci.category} in {ci.region}" if ci.region else ci.category,
        "Priority": str(position),
        "Recovery Time Objective": "",
    }
    return {column: values.get(column, "") for column in columns}


def _region_rows(cis: list[CI], columns: tuple[str, ...]) -> list[dict]:
    regions = sorted({ci.region for ci in cis if ci.region})
    rows = []
    for position, region in enumerate(regions):
        values = {"Designation": "Primary Site" if position == 0 else "Alternate Site",
                  "Site Name": region, "Site Type": "", "Address": ""}
        rows.append({column: values.get(column, "") for column in columns})
    return rows
