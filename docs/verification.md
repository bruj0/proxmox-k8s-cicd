# Verification matrix (WP07 T008 / T009 / T010)

Final verification of the Proxmox k3s pipeline against the SC-001
through SC-006 success criteria and NFR-010 through NFR-014
non-functional requirements.

This document is the source of truth for what "the pipeline works"
means. Each row lists the verification command, the expected
result, the responsible subsystem, and the live status.

## SC-001..SC-006 — Success criteria

| ID | Description | Command | Expected | Subsystem | Status |
|---|---|---|---|---|---|
| SC-001 | Clean-room end-to-end bring-up completes in <=60 minutes | `time (make build-image && cd infra/clusters/cicd && tofu apply -auto-approve && python ../../tools/bootstrap_cluster.py --cluster cicd && cd ../apps && tofu apply -auto-approve && python ../../tools/bootstrap_cluster.py --cluster apps)` | total wall-clock <= 60 min | SS1+SS2+SS3 | deferred (no live PVE host in CI) |
| SC-002 | PVC + Deployment succeeds on both clusters | `kubectl --context <cluster> apply -f tests/fixtures/pvc-deploy.yaml && sleep 90 && kubectl --context <cluster> get pvc,pods` | all PVCs `Bound`, all Pods `Ready` | SS2+SS3 | deferred (no live PVE host) |
| SC-003 | Ingress of class `cloudflare-tunnel` resolves via Cloudflare within 60 s | `kubectl --context apps apply -f tests/fixtures/ingress.yaml && sleep 60 && curl -sf -H "Host: app.example.com" https://151.80.34.63/` | HTTP 200 from Cloudflare-mediated path | SS3 | deferred |
| SC-004 | `nft list chain ip nat prerouting` shows zero new DNAT rules | `python tools/bootstrap_cluster.py --cluster cicd --phases host_ports` | exit 0, no new DNAT rules | SS3 | covered by unit test (`test_verify_no_new_dnat_rules_*`); live run deferred |
| SC-005 | Rerun idempotency: tofu + bootstrap converge to no-op in <60 s | `cd infra/clusters/cicd && tofu apply -auto-approve && python tools/bootstrap_cluster.py --cluster cicd` | both exit 0 in <60 s combined | SS2+SS3 | covered by state.json skip logic; live run deferred |
| SC-006 | `tofu destroy` cleans up | `cd infra/clusters/<name> && tofu destroy -auto-approve` | all VMs removed, context gone, no orphaned tokens | SS2 | covered by the decommission-cluster runbook |
| SC-007 | Phase 1 produces a Talos template at VMID 900 with `image-id.txt = "900\n"` on the live host | `ssh root@$PVE_HOST 'grep -E "^template:" /etc/pve/qemu-server/900.conf' && cat build/image-id.txt` | `template: 1`, `image-id.txt` is `900` | SS0+SS1 | **pass** (verified 2026-07-06 on BigBertha PVE 9.2.3; six Phase-1 patches from SKILL Step 1b applied) |

## NFR-010..NFR-014 — Non-functional requirements

| ID | Description | Verification | Status |
|---|---|---|---|
| NFR-010 | SKILL.md has YAML frontmatter with `name` and non-empty `description` | `pytest tools/tests/test_agent_skill.py::test_skill_md_frontmatter_has_name_and_description` passes | pass |
| NFR-011 | Skill idempotency (clean state vs partial state converge to the same end state) | `pytest tools/tests/test_agent_skill.py::test_skill_md_documents_rerun_and_partial_state` passes + live SC-005 rerun | pass (test) + deferred (live) |
| NFR-012 | Skill mentions every external library with version pin and rationale | `pytest tools/tests/test_agent_skill.py::test_skill_md_mentions_every_external_library_with_version` passes | pass |
| NFR-013 | Resource budget <= 16 vCPU + 24 GiB for default shape | Cluster module renders 3 control-plane (2 vCPU + 4 GiB each) + 1 worker (4 vCPU + 8 GiB) = 10 vCPU + 20 GiB; assert in `infra/modules/proxmox-k3s-cluster/tests/main.tftest.hcl` | pass (module test) |
| NFR-014 | Each new worker Ready in <5 min | `tests/fixtures/scale-worker.yaml` + timing assertion; live verification per `docs/runbooks/scale-workers.md` | deferred (live) |

## Live verification summary

Of the 7 SC + 5 NFR checks above:

- 4 are covered by automated unit/integration tests that pass as of
  the WP07 commit: NFR-010, NFR-011 (test side), NFR-012, NFR-013.
- 1 has both test coverage and live verification deferred: SC-004
  (host_ports; test exercises the diff algorithm, live needs PVE).
- 6 require a live PVE host: SC-001, SC-002, SC-003, SC-005 (live
  side), SC-006, NFR-014.
- 1 was verified live on 2026-07-06 (BigBertha PVE 9.2.3): SC-007
  (Phase 1 template bake; full details in the Phase-1 commit and in
  `.agents/skills/proxmox-k3s-pipeline/SKILL.md` Step 1b).

The deferred verifications are owned by the operator; the runbooks in
`docs/runbooks/` document the manual steps.

## Cross-cutting assertions

These assertions apply to every WP and are exercised by the project's
gates:

- **mypy**: `mypy --strict --explicit-package-bases -p tools` exits 0
  across all source files.
- **ruff**: `ruff check tools/` reports zero violations.
- **pytest**: `pytest tools/tests/` reports **69 passed** as of
  2026-07-06 (43 baseline WP00-WP06 + 8 WP07 acceptance + 11 Skill
  Step 0a/0b/0c/0d pins + **7 Step 1b Phase-1 pins** added on
  2026-07-06).
- **tofu tests**: `tofu test` passes for the four tofu modules
  (12 cluster-module + 2 cicd-instance + 5 apps-instance + **6 tokens,
  with the spec-T005 12-priv set extended to 19 privs to cover
  Phase 1 / Packer access**)
  = 25 passed / 0 failed).