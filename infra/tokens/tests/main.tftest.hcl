###############################################################################
# Terraform native tests for infra/tokens — run with `tofu test`.
#
# These tests use mocked providers so the suite runs without contacting
# Cloudflare or Proxmox. `mock_resource` defaults are only valid for
# *computed* attributes (the values returned by the API), so we set only
# `id`/`value` here and assert against the configured arguments
# (e.g. `proxmox_virtual_environment_role.k3s_cluster.privileges`) on the
# planned resource.
#
# Assertions covered:
#   1. Plan succeeds with sensible inputs.
#   2. Proxmox role contains exactly the spec T005 privilege set.
#   3. Proxmox ACL propagates to "/" with the k3s-cluster role.
#   4. Cloudflare scoped token contains exactly the spec T003 three
#      permission groups (Zone Read, Zone DNS Write, Cloudflare Tunnel:Edit).
#   5. Outputs expose the proxmox_token_id in canonical USER@REALM!TOKEN form.
#   6. The local_sensitive_file resource exists and points at output.json
#      with file_permission = "0600".
###############################################################################

mock_provider "cloudflare" {
  mock_data "cloudflare_account_api_token_permission_groups_list" {
    defaults = {
      result = [
        {
          id     = "00000000000000000000000000000001"
          name   = "Zone Read"
          scopes = ["com.cloudflare.api.account.zone.*"]
        },
        {
          id     = "00000000000000000000000000000002"
          name   = "Zone DNS Write"
          scopes = ["com.cloudflare.api.account.zone.*"]
        },
        {
          id     = "00000000000000000000000000000003"
          name   = "Cloudflare Tunnel:Edit"
          scopes = ["com.cloudflare.api.account"]
        },
      ]
    }
  }
  mock_resource "cloudflare_api_token" {
    defaults = {
      id    = "f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0"
      value = "scoped-token-secret-value"
    }
  }
}

mock_provider "proxmox" {
  mock_resource "proxmox_virtual_environment_role" {
    defaults = {
      id = "k3s-cluster"
    }
  }
  mock_resource "proxmox_virtual_environment_user" {
    defaults = {
      id = "k3s-terraform@pam"
    }
  }
  mock_resource "proxmox_acl" {
    defaults = {
      id = "k3s-terraform-acl"
    }
  }
  mock_resource "proxmox_user_token" {
    defaults = {
      id    = "k3s-terraform@pam!tf"
      value = "proxmox-token-secret-value"
    }
  }
}

mock_provider "local" {
  mock_resource "local_sensitive_file" {
    defaults = {
      id = "tokens-output-id"
    }
  }
}

variables {
  cloudflare_admin_token   = "test-cloudflare-admin-token-aaaaaaaaaaaaaaaaaaaa"
  cloudflare_account_id    = "11111111111111111111111111111111"
  cloudflare_zone_id       = "22222222222222222222222222222222"
  proxmox_api_url          = "https://pve.example.com:8006/api2/json"
  proxmox_api_token_id     = "root@pam!bootstrap"
  proxmox_api_token_secret = "bootstrap-secret-value"
  proxmox_endpoint         = "https://pve.example.com:8006/api2/json"
}

# ---------------------------------------------------------------------------
# 1. Plan succeeds and emits the expected resources.
# ---------------------------------------------------------------------------
run "plan_succeeds" {
  command = plan

  assert {
    condition     = cloudflare_api_token.k3s_scoped.name == "k3s-proxmox-terraform"
    error_message = "Cloudflare scoped token must be named 'k3s-proxmox-terraform'."
  }

  assert {
    condition     = proxmox_virtual_environment_role.k3s_cluster.role_id == "k3s-cluster"
    error_message = "Proxmox role id must be 'k3s-cluster'."
  }

  assert {
    condition     = proxmox_virtual_environment_user.k3s_terraform.user_id == "k3s-terraform@pam"
    error_message = "Proxmox user id must be 'k3s-terraform@pam'."
  }

  assert {
    condition     = proxmox_user_token.k3s_terraform_tf.token_name == "tf"
    error_message = "Proxmox user token name must be 'tf'."
  }
}

# ---------------------------------------------------------------------------
# 2. Proxmox role privilege set is exactly the spec T005 set.
# ---------------------------------------------------------------------------
run "proxmox_role_has_spec_t005_privileges" {
  command = plan

  assert {
    condition = alltrue([
      for p in [
        "VM.Allocate",
        "VM.Config.CPU",
        "VM.Config.Disk",
        "VM.Config.Memory",
        "VM.Config.Network",
        "VM.Config.Options",
        "VM.Console",
        "VM.PowerMgmt",
        "VM.Snapshot",
        "Datastore.AllocateSpace",
        "Datastore.Audit",
        "SDN.Use",
      ] :
      contains(proxmox_virtual_environment_role.k3s_cluster.privileges, p)
    ])
    error_message = "Proxmox role k3s-cluster is missing one or more spec T005 privileges."
  }

  # And no extras (the spec is the contract).
  assert {
    condition     = length(proxmox_virtual_environment_role.k3s_cluster.privileges) == 12
    error_message = "Proxmox role k3s-cluster must have exactly 12 privileges per spec T005."
  }
}

# ---------------------------------------------------------------------------
# 3. ACL is bound to "/" with propagate=true.
# ---------------------------------------------------------------------------
run "acl_binds_to_root_with_propagate" {
  command = plan

  assert {
    condition     = proxmox_acl.k3s_terraform.path == "/"
    error_message = "Proxmox ACL path must be '/' so cluster-wide privileges propagate."
  }

  assert {
    condition     = proxmox_acl.k3s_terraform.propagate == true
    error_message = "Proxmox ACL must propagate=true so child pools/datastores inherit."
  }

  assert {
    condition     = proxmox_acl.k3s_terraform.role_id == "k3s-cluster"
    error_message = "Proxmox ACL must reference the k3s-cluster role."
  }
}

# ---------------------------------------------------------------------------
# 4. Cloudflare scoped token contains exactly three policies covering the
#    spec T003 permission groups.
# ---------------------------------------------------------------------------
run "cloudflare_token_has_three_policies" {
  command = plan

  assert {
    condition     = length(cloudflare_api_token.k3s_scoped.policies) == 3
    error_message = "Cloudflare scoped token must expose exactly 3 policies (NFR-007)."
  }
}

# ---------------------------------------------------------------------------
# 5. proxmox_token_id output uses the canonical USER@REALM!TOKEN form.
# ---------------------------------------------------------------------------
run "proxmox_token_id_format" {
  command = plan

  assert {
    condition     = output.proxmox_token_id == "k3s-terraform@pam!tf"
    error_message = "proxmox_token_id output must be 'k3s-terraform@pam!tf' — got something else."
  }
}

# ---------------------------------------------------------------------------
# 6. local_sensitive_file.tokens_output exists with the right path + perms.
# ---------------------------------------------------------------------------
run "tokens_output_file_is_written" {
  command = plan

  assert {
    condition     = strcontains(local_sensitive_file.tokens_output.filename, "output.json")
    error_message = "local_sensitive_file.tokens_output must point at output.json."
  }

  assert {
    condition     = local_sensitive_file.tokens_output.file_permission == "0600"
    error_message = "output.json must be chmod 0600 (spec T007)."
  }
}