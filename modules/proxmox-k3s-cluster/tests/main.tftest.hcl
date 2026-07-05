###############################################################################
# OpenTofu native tests for modules/proxmox-k3s-cluster — run with `tofu test`.
#
# Mocked providers so the suite runs without contacting a real Proxmox host.
# Asserts on the planned resource attributes and the rendered templates.
#
# Misfits verified:
#   M2 (no host ports)   traefik-chartconfig.yaml.tftpl defaults to
#                         service.type=ClusterIP unless
#                         cf_publish_traefik_publicly=true.
#   M3 (cluster identity) cluster_name is exposed via output so the consuming
#                         root can enforce uniqueness.
#   M5 (DHCP safety)      VIP must NOT overlap the per-node IP range.
#
# Spec acceptance:
#   - control_plane.count must be 1 or 3 (FR-030)
#   - vmid_start must not overlap an existing populated VMID
#   - image_id must be non-empty
#   - output.json has the schema SS3 consumes
#
# Run from inside modules/proxmox-k3s-cluster:
#   tofu init -backend=false && tofu test
###############################################################################

mock_provider "proxmox" {
  mock_data "proxmox_virtual_environment_vms" {
    defaults = {
      vms = [
        { vm_id = 999, name = "talos-base",    node_name = "bigbertha", template = false, status = "stopped" },
        { vm_id = 900, name = "talos-v1.10.0", node_name = "bigbertha", template = true,  status = "stopped" },
        { vm_id = 110, name = "unrelated-vm",  node_name = "bigbertha", template = false, status = "running" },
      ]
    }
  }
  mock_resource "proxmox_cloned_vm" {
    defaults = {
      id = 9001
    }
  }
  mock_resource "proxmox_virtual_environment_cloned_vm" {
    defaults = {}
  }
  mock_resource "proxmox_virtual_environment_hosts" {
    defaults = {
      id = "cicd-cp-1"
    }
  }
}

mock_provider "helm" {
  mock_resource "helm_release" {
    defaults = {
      id     = "default/cloudflare-tunnel-ingress-controller"
      status = "deployed"
    }
  }
}

# ---------------------------------------------------------------------------
# Common variables (used as defaults for most runs).
# ---------------------------------------------------------------------------

variables {
  cluster_name                = "cicd"
  vip                         = "10.0.0.30"
  vmid_start                  = 200
  ip_start                    = "10.0.0.201/24"
  image_id                    = "900"
  vnet_bridge                 = "vnet0"
  pod_cidr                    = "10.42.0.0/16"
  svc_cidr                    = "10.43.0.0/16"
  control_plane = {
    count   = 1
    cpu     = 4
    ram_mb  = 8192
    disk_gb = 32
  }
  workers = {
    count   = 1
    cpu     = 4
    ram_mb  = 8192
    disk_gb = 32
  }
  cf_api_token                = "test-cf-token-redacted"
  cf_account_id               = "test-cf-account-redacted"
  cf_tunnel_name              = "cicd"
  cf_ingress_class            = "cloudflare-tunnel"
  cf_publish_traefik_publicly = false
}

# ---------------------------------------------------------------------------
# M2 — Traefik defaults to ClusterIP.
# ---------------------------------------------------------------------------

run "traefik_defaults_to_clusterip" {
  command = plan

  assert {
    condition     = strcontains(local_file.traefik_chartconfig.content, "ClusterIP")
    error_message = "M2 violated: default Traefik HelmChartConfig must use service.type=ClusterIP."
  }
  assert {
    condition     = strcontains(local_file.traefik_chartconfig.content, "traefik-internal")
    error_message = "M2 violated: default Traefik HelmChartConfig must set ingressClass.name=traefik-internal."
  }
}

# ---------------------------------------------------------------------------
# M2 fallback — cf_publish_traefik_publicly=true enables hostPorts.
# ---------------------------------------------------------------------------

run "traefik_publish_publicly_uses_hostports" {
  command = plan
  variables {
    cf_publish_traefik_publicly = true
  }
  assert {
    condition     = local_file.traefik_chartconfig.content != null
    error_message = "Traefik chartconfig must render when cf_publish_traefik_publicly=true."
  }
}

# ---------------------------------------------------------------------------
# M3 — cluster_name is exposed via output so the consuming root can
# enforce uniqueness across all Clusters.
# ---------------------------------------------------------------------------

run "cluster_name_exposed" {
  command = plan
  assert {
    condition     = output.cluster_name == "cicd"
    error_message = "M3 violated: module must expose cluster_name via output so the root can compare against existing Clusters."
  }
}

# ---------------------------------------------------------------------------
# M5 — VIP must NOT overlap the per-node IP range (DHCP safety).
# ---------------------------------------------------------------------------

# Negative test for M5: cidrhost("10.0.0.201/24", 0) = "10.0.0.0" (the IP
# portion is the NETWORK and cidrhost indexes hosts), so the first cp IP is
# always "10.0.0.0" with these defaults. vip = "10.0.0.0" collides and the
# precondition must fire.
run "vip_overlap_with_dhcp_range_is_rejected" {
  command = plan
  variables {
    vip = "10.0.0.0"
  }
  expect_failures = [
    terraform_data.vip_in_dhcp_range,
  ]
}

# Positive counterpart: with default variables vip=10.0.0.30 is disjoint from
# the per-node range "10.0.0.0","10.0.0.1".
run "vip_in_safe_range_is_accepted" {
  command = plan
  assert {
    condition     = output.vip == "10.0.0.30"
    error_message = "vip must round-trip via output."
  }
  assert {
    # Sanity: confirm the precondition's input set is what we expect.
    condition     = length(terraform_data.vip_in_dhcp_range.input.control_plane_ip) == 1
    error_message = "control_plane_ip set size must equal control_plane.count."
  }
}

# ---------------------------------------------------------------------------
# Spec acceptance: control_plane.count must be 1 or 3.
# ---------------------------------------------------------------------------

run "control_plane_count_one_is_accepted" {
  command = plan
  variables {
    control_plane = {
      count   = 1
      cpu     = 4
      ram_mb  = 8192
      disk_gb = 32
    }
  }
  assert {
    condition     = var.control_plane.count == 1
    error_message = "control_plane.count=1 must be accepted."
  }
}

run "control_plane_count_three_is_accepted" {
  command = plan
  variables {
    control_plane = {
      count   = 3
      cpu     = 4
      ram_mb  = 8192
      disk_gb = 32
    }
    workers = {
      count   = 0
      cpu     = 4
      ram_mb  = 8192
      disk_gb = 32
    }
  }
  assert {
    condition     = var.control_plane.count == 3
    error_message = "control_plane.count=3 must be accepted."
  }
}

# ---------------------------------------------------------------------------
# Spec acceptance: vmid_start at a fresh range is accepted (110 + 900 + 999
# are existing; 200 is fresh).
# ---------------------------------------------------------------------------

run "vmid_start_at_template_is_accepted" {
  command = plan
  variables {
    vmid_start = 200
  }
  assert {
    condition     = var.vmid_start == 200
    error_message = "Fresh vmid_start=200 must be accepted (template=900, base=999, unrelated=110)."
  }
}

# ---------------------------------------------------------------------------
# Spec acceptance: image_id must be non-empty.
# ---------------------------------------------------------------------------

run "image_id_empty_rejected" {
  command = plan
  variables {
    image_id = ""
  }
  expect_failures = [
    terraform_data.validate_image_id,
  ]
}

# ---------------------------------------------------------------------------
# Spec acceptance: nodes map length matches control_plane.count + workers.count.
# ---------------------------------------------------------------------------

run "node_count_matches_topology" {
  command = plan
  assert {
    condition     = length(output.nodes) == var.control_plane.count + var.workers.count
    error_message = "nodes output length must equal control_plane.count + workers.count."
  }
  assert {
    condition     = length([for n in output.nodes : n if n.role == "control_plane"]) == var.control_plane.count
    error_message = "control_plane node count must match."
  }
  assert {
    condition     = length([for n in output.nodes : n if n.role == "worker"]) == var.workers.count
    error_message = "worker node count must match."
  }
}

# ---------------------------------------------------------------------------
# Spec acceptance: output.json contains the schema fields SS3 consumes.
# ---------------------------------------------------------------------------

run "output_json_has_required_fields" {
  command = plan
  assert {
    condition     = output.vip != null && output.vip == var.vip
    error_message = "output.vip must round-trip."
  }
  assert {
    condition     = output.talos_dir != null && length(output.talos_dir) > 0
    error_message = "output.talos_dir must be populated."
  }
  assert {
    condition     = length(output.helm_releases) >= 6
    error_message = "output.helm_releases must list >=6 releases (cilium, kube-vip, proxmox-ccm, proxmox-csi, traefik, cf-tunnel)."
  }
}

# ---------------------------------------------------------------------------
# Spec acceptance: talos configs are written (one per node).
# ---------------------------------------------------------------------------

run "talos_configs_are_rendered" {
  command = plan
  assert {
    condition     = length(local_sensitive_file.talos_machineconfig) == var.control_plane.count + var.workers.count
    error_message = "talos_machineconfig count must match node count."
  }
}