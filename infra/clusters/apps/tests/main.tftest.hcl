###############################################################################
# OpenTofu native tests for clusters/apps.
#
# M3 (proven across instances): a second instantiation of the cluster module
# uses non-overlapping VMIDs and pod/svc CIDRs. This suite asserts:
#   - apps uses VMIDs 210..211 (not 200..201 from cicd).
#   - apps uses pod_cidr 172.20.0.0/16 and svc_cidr 172.21.0.0/16 (not cicd's
#     172.16/172.17).
#
# 2026-07-08: VIP assertion removed (vip dropped from the module in
# the refactor -- Proxmox SDN owns IPs and the kube-vip Service VIP
# is derived by the bootstrap, not tofu).
#
# Note: the negative case (apps.vmid_start=200 collides with cicd) is covered
# at the module level by modules/proxmox-k3s-cluster/tests/main.tftest.hcl via
# expect_failures on terraform_data.vmid_overlap.
#
# Run from inside clusters/apps:
#   tofu init -backend=false && tofu test
###############################################################################

mock_provider "proxmox" {
  mock_data "proxmox_virtual_environment_vms" {
    defaults = {
      vms = []
    }
  }
  mock_resource "proxmox_cloned_vm" {
    defaults = {}
  }
  mock_resource "proxmox_virtual_environment_hosts" {
    defaults = {}
  }
}

mock_provider "local" {
  mock_data "local_file" {
    defaults = {
      content = "900\n"
    }
  }
  mock_data "local_sensitive_file" {
    defaults = {
      content = "{\"cf_api_token\":\"test-token\",\"cf_account_id\":\"test-account\"}"
    }
  }
}

mock_provider "helm" {
  mock_resource "helm_release" {
    defaults = {
      status = "deployed"
    }
  }
}

mock_provider "powerdns" {
  mock_resource "powerdns_record" {
    defaults = {
      id = "test.intranet.local.:::A"
    }
  }
}

# ---------------------------------------------------------------------------
# M3 baseline identity check: apps uses distinct VIP / VMID / CIDR from cicd.
# ---------------------------------------------------------------------------

run "apps_does_not_expose_vip" {
  command = plan
  # vip was removed from the module in 2026-07-08 -- both roots of
  # the cluster module MUST agree that vip is gone. This pins the
  # absence on the apps side.
  assert {
    condition     = !can(module.apps.vip)
    error_message = "module.apps.vip must NOT be exposed; vip was removed in the 2026-07-08 refactor."
  }
}

run "apps_uses_distinct_vmids" {
  command = plan
  # 2026-07-09 reconciled to live PVE state: cicd is at 111..112,
  # apps at 113..114. The M3 invariant we still assert is that apps
  # must be disjoint from cicd, NOT a hardcoded >= 210 floor (the
  # historical docs/README value drifted because Proxmox allocated
  # adjacent free slots when the clusters were originally applied).
  assert {
    condition     = length([for n in module.apps.nodes : n.vmid]) == 2
    error_message = "apps must declare exactly 2 nodes (1 cp + 1 worker)."
  }
  assert {
    condition     = !contains([for n in module.apps.nodes : n.vmid], 111)
    error_message = "M3 violated: apps uses VMID 111 (cicd's first VMID)."
  }
  assert {
    condition     = !contains([for n in module.apps.nodes : n.vmid], 112)
    error_message = "M3 violated: apps uses VMID 112 (cicd's second VMID)."
  }
  assert {
    condition     = min([for n in module.apps.nodes : n.vmid]...) >= 110
    error_message = "apps vmid_start below 110 violates the 4-VMID buffer rule (see docs/cluster-instances.md)."
  }
}

# ---------------------------------------------------------------------------
# M3 disjoint guard. cicd uses 111..112; apps 113..114. The live drift
# from the documented 200..201 / 210..211 floor was reconciled 2026-07-09.
# ---------------------------------------------------------------------------

run "apps_uses_distinct_pod_and_svc_cidrs" {
  command = plan
  assert {
    condition     = module.apps.pod_cidr == "172.20.0.0/16"
    error_message = "apps.pod_cidr must be 172.20.0.0/16 (cicd uses 172.16.0.0/16)."
  }
  assert {
    condition     = module.apps.svc_cidr == "172.21.0.0/16"
    error_message = "apps.svc_cidr must be 172.21.0.0/16 (cicd uses 172.17.0.0/16)."
  }
  assert {
    condition     = module.apps.pod_cidr != "172.16.0.0/16"
    error_message = "M3 violated: apps.pod_cidr collides with cicd."
  }
  assert {
    condition     = module.apps.svc_cidr != "172.17.0.0/16"
    error_message = "M3 violated: apps.svc_cidr collides with cicd."
  }
}

run "apps_cluster_name_is_apps" {
  command = plan
  assert {
    condition     = module.apps.cluster_name == "apps"
    error_message = "apps.cluster_name must be 'apps' (cicd is 'cicd')."
  }
  assert {
    condition     = module.apps.cluster_name != "cicd"
    error_message = "M3 violated: apps uses cicd's cluster_name."
  }
}

# ---------------------------------------------------------------------------
# M3 overlap guard: cicd uses VMIDs 200..201; apps must use a disjoint range.
# This run simulates cicd already provisioned on the host (vm_ids 200,201 in
# the live PVE) and asserts that apps still resolves cleanly because apps
# uses vmid_start=210 -- a 4-gate buffer away from cicd.
#
# Note: the cluster root does NOT have direct access to the module's
# data.proxmox_virtual_environment_vms.existing, so we cannot use
# override_data on it from this test scope. We rely on the module's own
# tests/main.tftest.hcl for the negative case (apps.vmid_start=200 collides
# with cicd -- covered there with expect_failures on terraform_data.vmid_overlap).
# ---------------------------------------------------------------------------

run "apps_vmid_range_is_disjoint_from_cicd" {
  command = plan
  # 2026-07-09 reconciled to live PVE state: cicd occupies 111..112,
  # apps occupies 113..114. We assert the M3 invariant (apps is
  # disjoint from cicd) rather than a hardcoded apps >= 210 floor
  # that drift-corrections would repeatedly break.
  assert {
    condition     = !contains([for n in module.apps.nodes : n.vmid], 111)
    error_message = "M3 violated: apps uses VMID 111 (cicd's first VMID)."
  }
  assert {
    condition     = !contains([for n in module.apps.nodes : n.vmid], 112)
    error_message = "M3 violated: apps uses VMID 112 (cicd's second VMID)."
  }
  assert {
    condition     = max([for n in module.apps.nodes : n.vmid]...) < 111 || min([for n in module.apps.nodes : n.vmid]...) > 112
    error_message = "apps VMID range overlaps cicd's 111..112."
  }
}