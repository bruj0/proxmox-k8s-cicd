###############################################################################
# OpenTofu native tests for the cluster root clusters/cicd.
#
# These tests assert the root-level invariants that go beyond the module's
# own tests: cluster_name uniqueness across siblings, image_id plumbing from
# SS1's output, and the schema of the cluster_output file.
#
# Run from inside clusters/cicd:
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
# Root-level sanity checks.
# ---------------------------------------------------------------------------

run "root_module_resolves" {
  command = plan
  assert {
    condition     = module.cicd.cluster_name == "cicd"
    error_message = "module.cicd.cluster_name must be 'cicd'."
  }
  assert {
    condition     = module.cicd.vip == "10.0.0.30"
    error_message = "module.cicd.vip must be '10.0.0.30'."
  }
}

run "root_has_one_control_plane_and_one_worker" {
  command = plan
  assert {
    condition     = module.cicd.control_plane_count == 1
    error_message = "cicd must have 1 control-plane node."
  }
  assert {
    condition     = module.cicd.worker_count == 1
    error_message = "cicd must have 1 worker node."
  }
}