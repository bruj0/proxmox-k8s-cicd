###############################################################################
# Cluster root variables — kept thin. All inputs to the module are explicit;
# the root adds nothing new, only wires data sources into module inputs.
#
# M7/NFR-007: secrets come from SS0's output.json via local_sensitive_file.
# They are NEVER hardcoded in terraform.tfvars and NEVER committed.
###############################################################################

# Intentionally empty for now. Future WPs may add per-root overrides
# (e.g. production vs staging might pin different cf_tunnel_name).

variable "powerdns_api_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "PowerDNS API key. Sourced from TF_VAR_powerdns_api_key (set by scripts/apply_tofu.py from POWERDNS_API_KEY in .env). Empty disables DNS record creation -- the rest of the cluster still applies."
}