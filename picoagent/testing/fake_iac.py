"""Synthetic infrastructure-as-code fixtures for the ``iscp-author`` plugin's tests.

Everything here is invented: no real OCIDs, ARNs, account numbers, hostnames or addresses.
The *shapes* are real - the OCI sample mirrors the layout of a production Oracle Cloud DR
estate (a root module with a ``locals`` instance map and ``for_each`` over it, two aliased
providers, module calls, a compute module with volume groups that carry cross-region
replicas, a database module with a Data Guard association) because the scanner has to cope
with that layout, and the AWS sample covers the equivalent constructs plus one deliberately
unknown resource type so the "unrecognised, not guessed at" path is exercised.

Importable on its own::

    from picoagent.testing.fake_iac import write_sample_project
    terraform_dir = write_sample_project(tmp_path)
"""
from __future__ import annotations

import json
from pathlib import Path

#: Three files mimicking a real root-module-plus-modules layout. Between them they contain a
#: literal ``locals`` map driving ``for_each``, two provider aliases, a module call, nested
#: blocks, a heredoc, block and line comments, a string containing an unbalanced ``{``, a
#: ternary ``count``, and a volume group carrying a cross-region replica.
OCI_SAMPLE_TF: dict[str, str] = {
    "main.tf": '''\
####################################################################
# main.tf - root module. The brace in this comment { is a decoy.
####################################################################

provider "oci" {
  alias  = "ashburn"
  region = "us-ashburn-1"
}

provider "oci" {
  alias  = "phoenix"
  region = "us-phoenix-1"
}

locals {
  app_shape = "VM.Standard.E5.Flex"

  primary_instances = {
    "PRI-APPWEB01" = {
      role             = "web"
      logical_hostname = "appweb01"
      shape            = local.app_shape
    }
    "PRI-APPWEB02" = {
      role             = "web"
      logical_hostname = "appweb02"
      shape            = local.app_shape
    }
    "PRI-APPJOB01" = {
      role             = "job"
      logical_hostname = "appjob01"
      shape            = local.app_shape
    }
  }
}

resource "oci_core_vcn" "primary" {
  provider       = oci.ashburn
  display_name   = "vcn-primary"
  cidr_blocks    = ["10.20.0.0/16"]
  # A string that contains a brace must not end the block: "not a close } brace"
  freeform_tags  = { "note" = "contains } brace" }
}

resource "oci_dns_steering_policy" "failover" {
  provider     = oci.ashburn
  display_name = "steer-app-failover"
  template     = "FAILOVER"
  depends_on   = [oci_core_vcn.primary]
}

module "compute" {
  source = "./modules/compute"
  providers = {
    oci.ashburn = oci.ashburn
    oci.phoenix = oci.phoenix
  }
}

module "database" {
  source = "./modules/database"
}
''',

    "modules/compute/main.tf": '''\
/*
 * modules/compute - application tier and its replicated data volumes.
 * This block comment closes here: */

resource "oci_core_instance" "primary" {
  provider     = oci.ashburn
  for_each     = local.primary_instances
  display_name = each.key
  state        = "RUNNING"

  shape_config {
    ocpus         = 2
    memory_in_gbs = 16
  }

  source_details {
    source_type = "image"
  }

  metadata = {
    user_data = "placeholder"
  }
}

resource "oci_core_volume_group" "primary_data" {
  provider     = oci.ashburn
  display_name = "vg-primary-data"

  source_details {
    type = "volumeIds"
  }

  # Cross-region replication lives on this same resource - there is no separate replica
  # resource in the provider.
  volume_group_replicas {
    availability_domain = "us-phoenix-1-AD-1"
    display_name        = "vg-primary-data-replica-phx"
  }
}

resource "oci_objectstorage_replication_policy" "artifacts" {
  provider            = oci.ashburn
  name                = "artifacts-to-phoenix"
  destination_region  = "us-phoenix-1"
  destination_bucket  = "artifacts-dr"
}

resource "oci_core_instance" "bastion" {
  provider     = oci.ashburn
  display_name = "bastion"
  count        = var.enable_bastion ? 1 : 0

  extended_metadata = {
    startup = <<-EOT
      # this heredoc contains braces { and } and a fake resource "oci_core_vcn" "decoy" {
      echo starting
    EOT
  }
}
''',

    "modules/database/main.tf": '''\
# modules/database - primary database and its standby association.

resource "oci_database_db_system" "primary" {
  provider     = oci.ashburn
  display_name = "db-primary"
  shape        = var.db_shape
}

resource "oci_database_data_guard_association" "standby" {
  provider                 = oci.phoenix
  display_name             = "dg-primary-to-standby"
  protection_mode          = "MAXIMUM_AVAILABILITY"
  transport_type           = "ASYNC"
  depends_on               = [oci_database_db_system.primary]
}

resource "oci_monitoring_alarm" "lag" {
  provider     = oci.ashburn
  display_name = "alarm-transport-lag"
  count        = var.enable_alarms ? 1 : 0
}
''',
}

#: AWS equivalents, including one type the category table deliberately does not know.
AWS_SAMPLE_TF: dict[str, str] = {
    "aws.tf": '''\
provider "aws" {
  alias  = "primary"
  region = "us-east-1"
}

resource "aws_instance" "app" {
  provider      = aws.primary
  instance_type = "m6i.large"
  tags = {
    Name = "app-primary"
  }
}

resource "aws_db_instance" "replica" {
  provider            = aws.primary
  identifier          = "app-db-replica"
  replicate_source_db = "app-db-primary"
}

resource "aws_s3_bucket" "artifacts" {
  provider = aws.primary
  bucket   = "app-artifacts"
}

resource "aws_s3_bucket_replication_configuration" "artifacts" {
  provider   = aws.primary
  bucket     = "app-artifacts"
  depends_on = [aws_s3_bucket.artifacts]
}

resource "aws_lb" "app" {
  provider           = aws.primary
  name               = "app-alb"
  load_balancer_type = "application"
}

resource "aws_route53_record" "app" {
  provider = aws.primary
  name     = "app.example.invalid"
  type     = "A"
}

resource "aws_backup_plan" "nightly" {
  provider = aws.primary
  name     = "nightly-plan"
}

resource "aws_made_up_thing" "mystery" {
  provider = aws.primary
  name     = "nobody-knows"
}
''',
}

#: ``terraform show -json`` for the OCI sample, in the structure HashiCorp documents at
#: https://developer.hashicorp.com/terraform/internals/json-format - ``values.root_module``
#: with ``resources[]`` (address/mode/type/name/index/provider_name/values) and
#: ``child_modules[]`` repeating that shape. Values are resolved, unlike the HCL scan.
TERRAFORM_SHOW_JSON: dict = {
    "format_version": "1.0",
    "terraform_version": "1.9.5",
    "values": {
        "root_module": {
            "resources": [
                {"address": "oci_core_vcn.primary", "mode": "managed", "type": "oci_core_vcn",
                 "name": "primary", "provider_name": "registry.terraform.io/oracle/oci",
                 "schema_version": 0,
                 "values": {"display_name": "vcn-primary", "cidr_blocks": ["10.20.0.0/16"]}},
                {"address": "oci_dns_steering_policy.failover", "mode": "managed",
                 "type": "oci_dns_steering_policy", "name": "failover",
                 "provider_name": "registry.terraform.io/oracle/oci", "schema_version": 0,
                 "values": {"display_name": "steer-app-failover", "template": "FAILOVER"}},
            ],
            "child_modules": [
                {"address": "module.compute", "resources": [
                    {"address": 'module.compute.oci_core_instance.primary["PRI-APPWEB01"]',
                     "mode": "managed", "type": "oci_core_instance", "name": "primary",
                     "index": "PRI-APPWEB01", "provider_name": "registry.terraform.io/oracle/oci",
                     "schema_version": 0,
                     "values": {"display_name": "PRI-APPWEB01", "shape": "VM.Standard.E5.Flex",
                                "state": "RUNNING", "region": "us-ashburn-1"}},
                    {"address": "module.compute.oci_core_volume_group.primary_data",
                     "mode": "managed", "type": "oci_core_volume_group", "name": "primary_data",
                     "provider_name": "registry.terraform.io/oracle/oci", "schema_version": 0,
                     "values": {"display_name": "vg-primary-data", "region": "us-ashburn-1",
                                "volume_group_replicas": [
                                    {"availability_domain": "us-phoenix-1-AD-1",
                                     "display_name": "vg-primary-data-replica-phx"}]}},
                ]},
                {"address": "module.database", "resources": [
                    {"address": "module.database.oci_database_data_guard_association.standby",
                     "mode": "managed", "type": "oci_database_data_guard_association",
                     "name": "standby", "provider_name": "registry.terraform.io/oracle/oci",
                     "schema_version": 0,
                     "values": {"display_name": "dg-primary-to-standby", "region": "us-phoenix-1",
                                "protection_mode": "MAXIMUM_AVAILABILITY"}},
                ], "child_modules": []},
            ],
        }
    },
}

#: A small CloudFormation template in JSON. The YAML refusal path is tested separately.
CFN_SAMPLE_JSON: dict = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Resources": {
        "AppServer": {"Type": "AWS::EC2::Instance",
                      "Properties": {"InstanceType": "m6i.large", "ImageId": "ami-invalid"}},
        "AppDatabase": {"Type": "AWS::RDS::DBInstance", "DependsOn": "AppServer",
                        "Properties": {"DBInstanceClass": "db.m6i.large", "Engine": "postgres"}},
        "ArtifactBucket": {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": "app-artifacts"}},
    },
}


def write_sample_project(root: Path) -> Path:
    """Write the OCI and AWS Terraform samples under ``root/terraform`` and return that path."""
    terraform = root / "terraform"
    for name, body in {**OCI_SAMPLE_TF, **AWS_SAMPLE_TF}.items():
        path = terraform / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return terraform


def write_terraform_show_json(root: Path) -> Path:
    path = root / "tfshow.json"
    path.write_text(json.dumps(TERRAFORM_SHOW_JSON, indent=2), encoding="utf-8")
    return path


def write_cloudformation_json(root: Path) -> Path:
    path = root / "stack.json"
    path.write_text(json.dumps(CFN_SAMPLE_JSON, indent=2), encoding="utf-8")
    return path


#: A complete answers dict for a fictional cloud service offering, used by the render tests.
#: Nothing here refers to a real organisation, person, address or system.
SAMPLE_ANSWERS: dict = {
    "csp.name": "Northwind Systems, Inc.",
    "cso.name": "Northwind Ledger Cloud",
    "cso.abbreviation": "NLC",
    "cso.fedramp_id": "FR0000000000",
    "csp.poc": "the Northwind Systems ISSO",
    "doc.version": "2.1",
    "doc.date": "03/14/2026",
    "prepared_by.organization": "Northwind Systems, Inc.",
    "prepared_by.street": "1 Example Way",
    "prepared_by.suite": "Suite 400",
    "prepared_by.city_state_zip": "Springfield, ZZ 00000",
    "prepared_for.organization": "Northwind Systems, Inc.",
    "prepared_for.street": "1 Example Way",
    "prepared_for.suite": "Suite 400",
    "prepared_for.city_state_zip": "Springfield, ZZ 00000",
    "doc.revisions": [
        {"Date": "03/14/2026", "Description": "Annual review", "Version": "2.1",
         "Author": "Contingency Planning Coordinator"},
    ],
    "approval1.name": "A. Example",
    "approval1.title": "Contingency Planning Director",
    "approval1.date": "03/14/2026",
    "approval2.name": "B. Example",
    "approval2.title": "System Owner",
    "approval2.date": "03/14/2026",
    "approval3.name": "C. Example",
    "approval3.title": "Authorizing Official Representative",
    "approval3.date": "03/14/2026",
    "scope.impact_level": "Moderate",
    "scope.rto_hours": 12,
    "scope.short_term_disruption": "4 hours",
    "scope.other_plans": [
        {"Plan Name": "Incident Response Plan", "Mission/Purpose": "Response to security incidents"},
    ],
    "assumptions.ups_runtime": "20 minutes",
    "assumptions.generator_start": "90 seconds",
    "assumptions.offsite_location": "Springfield, ZZ",
    "system.description": "Two-region deployment of the Northwind Ledger Cloud application and "
                          "database tiers, described in full in SSP section 9.",
    "backup.software": "Vendor-supplied volume snapshot service",
    "backup.hardware": "Provider-managed block storage",
    "backup.frequency": "Hourly incremental, nightly full",
    "backup.type": "Incremental Backup",
    "backup.retention": "35 days",
    "backup.storage_site_name": "Alternate region object store",
    "backup.storage_street": "2 Example Road",
    "backup.storage_city_state_zip": "Shelbyville, ZZ 00001",
    "sites": [
        {"Designation": "Primary Site", "Site Name": "us-ashburn-1", "Site Type": "Hot Sites",
         "Address": "Provider region, Northern Virginia"},
        {"Designation": "Alternate Site", "Site Name": "us-phoenix-1", "Site Type": "Warm Sites",
         "Address": "Provider region, Arizona"},
    ],
    "roles.plc_purchase_limit": "$50,000",
    "activation.authorized": [
        {"Name": "A. Example", "Title and ISCP Role": "Contingency Planning Director",
         "Contact Information": "a.example@northwind.invalid"},
    ],
    "notification.procedures": "The Contingency Planning Director notifies the Coordinator, who "
                               "pages the team through the on-call rotation.",
    "outage.assessor_role": "the Outage and Damage Assessment Lead",
    "outage.procedures": "Confirm the region status page, check replication lag, and record the "
                         "last successful transaction before declaring.",
    "recovery.sequence": [
        "Confirm the alternate region is healthy",
        "Promote the standby database",
        "Activate the replicated volume groups",
        "Start the application tier",
        "Steer traffic",
    ],
    "recovery.procedures": "Keystroke-level procedures are in the runbooks generated alongside this "
                           "plan: RB-01 switchover, RB-02 failover, RB-03 failback, RB-04 drill.",
    "recovery.escalation": "The Coordinator reports status every 30 minutes to the Director, who "
                           "escalates to executive management if the RTO is at risk.",
    "reconstitution.data_validation": "Compare the database audit log against the recovered database "
                                       "and confirm every committed transaction is present.",
    "reconstitution.functional_validation": "Run the regression suite against the recovered "
                                             "environment.",
    "reconstitution.declaring_role": "the Contingency Planning Director",
    "reconstitution.user_notification": "Customers are notified through the status page and the "
                                         "contractual notification list.",
    "cleanup.procedures": "Decommission temporary capacity and restore the normal replication "
                          "posture.",
    "cleanup.responsibilities": [
        {"Role": "Software Recovery Team", "Cleanup Responsibilities": "Decommission drill instances"},
    ],
    "media_return.procedures": "Not applicable; backups are provider-managed and never leave the "
                               "provider's storage.",
    "restored_backup.procedures": "Trigger an unscheduled full backup and confirm it completes.",
    "event_doc.responsibilities": [
        {"Role Name": "Contingency Planning Coordinator", "Documentation Responsibility":
         "Activity log"},
    ],
    "testing.procedures": "An annual non-disruptive drill following RB-04, plus a tabletop exercise "
                          "each half-year.",
    "contacts.key_personnel": [
        {"Role": "Contingency Plan Director", "Name and Home Address": "A. Example",
         "Email": "a.example@northwind.invalid", "Phone": "Primary: 000-000-0000"},
    ],
    "contacts.vendors": [
        {"Vendor": "Cloud provider",
         "Product or Service License #, Contract #, Account #, or SLA": "Enterprise agreement",
         "Phone": "Primary: 000-000-0001"},
    ],
    "alt_storage.address": "Alternate provider region",
    "alt_storage.distance": "Approximately 3,000 km",
    "alt_storage.ownership": "Third-party cloud provider",
    "alt_storage.poc": "Cloud provider support",
    "alt_storage.delivery_schedule": "Continuous asynchronous replication",
    "alt_storage.retrieval_procedures": "Restore through the provider console or CLI",
    "alt_storage.authorized_personnel": "Software Recovery Team leads",
    "alt_storage.accessibility_problems": "None; access is over the network",
    "alt_storage.mitigation_steps": "Second network path through a separate provider",
    "alt_storage.data_types": "Database backups, application artifacts, configuration",
    "alt_processing.address": "Alternate provider region",
    "alt_processing.distance": "Approximately 3,000 km",
    "alt_processing.ownership": "Third-party cloud provider",
    "alt_processing.poc": "Cloud provider support",
    "alt_processing.access_procedures": "Federated console access with hardware MFA",
    "alt_processing.authorized_personnel": "Hardware and Software Recovery Team leads",
    "alt_processing.site_type": "Warm Sites",
    "alt_processing.mitigation_steps": "Pre-provisioned capacity reservation",
    "alt_telecom.vendors": "Two independent transit providers",
    "alt_telecom.agreements": "Committed-rate contracts with both",
    "alt_telecom.capacity": "10 Gbps per path",
    "alt_telecom.authorized_personnel": "Telecommunications Team lead",
    "alt_processing_procedures": "No manual work-around exists; the service is fully automated.",
    "validation.test_plan": [
        {"Procedure": "Sign in to the recovered application", "Expected Results": "Dashboard loads",
         "Actual Results": "", "Successful?": "", "Performed by": ""},
    ],
    "test_report.name": "Annual contingency plan test",
    "test_report.system_name": "Northwind Ledger Cloud",
    "test_report.date": "02/10/2026",
    "test_report.lead": "Contingency Planning Coordinator",
    "test_report.location": "Remote",
    "test_report.participants": "Contingency Plan Team",
    "test_report.components": "Database, application tier, DNS",
    "test_report.assumptions": "Primary region assumed lost",
    "test_report.objectives": ["Assess effectiveness of procedures",
                               "Assess effectiveness of notification procedures"],
    "test_report.methodology": "Non-disruptive drill in an isolated copy of the alternate region",
    "test_report.activities_results": "All steps completed within the RTO",
    "test_report.post_test_actions": "Automate the DNS steering step",
    "test_report.lessons_learned": "Manual DNS steering was the longest step",
    "test_report.recommended_changes": "Add a pre-staged steering policy to RB-02",
    "test_schedule": "Plan reviewed annually each January; drill each February; tabletop each August.",
    "associated_plans": [
        {"System Name": "Northwind Identity Service", "Plan Name": "Northwind Identity ISCP"},
    ],
    "bia.completion_date": "01/20/2026",
    "bia.system_description": "The Northwind Ledger Cloud application and database tiers, deployed "
                              "across two provider regions.",
    "bia.processes": [
        {"Mission/Business Process": "Post ledger entry",
         "Description": "Recording a financial transaction against a customer account"},
    ],
    "bia.impact_categories": [
        {"Impact category": "Cost", "Severe": "greater than $1 million", "Moderate": "$100k-$1 million",
         "Minimal": "under $100k"},
    ],
    "bia.process_impacts": [
        {"Mission/Business Process": "Post ledger entry", "Cost": "Severe",
         "Impact": "Customers cannot record transactions"},
    ],
    "bia.downtime": [
        {"Mission/Business Process": "Post ledger entry", "MTD": "24 hours", "RTO": "12 hours",
         "RPO": "1 hour"},
    ],
    "bia.downtime_drivers": "The MTD is set by the customer contract; the RTO is what the "
                            "cross-region architecture sustains in drills.",
    "bia.alternate_means": "None. There is no manual work-around for ledger posting.",
    "bia.resources": [
        {"System Resource/Component": "CI-0001 application tier",
         "Platform/OS/Version (as applicable)": "oci_core_instance", "Description": "compute"},
    ],
    "bia.priorities": [
        {"Priority": "1", "System Resource/Component": "CI-0002 database",
         "Recovery Time Objective": "4 hours to promote the standby"},
    ],
    "bia.alternate_strategies": "Warm standby capacity is reserved in the alternate region.",
}
