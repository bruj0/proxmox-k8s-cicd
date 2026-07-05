###############################################################################
# Cluster root variables — kept thin. All inputs to the module are explicit;
# the root adds nothing new, only wires data sources into module inputs.
#
# M7/NFR-007: secrets come from SS0's output.json via local_sensitive_file.
# They are NEVER hardcoded in terraform.tfvars and NEVER committed.
###############################################################################

# Intentionally empty for now. Future WPs may add per-root overrides
# (e.g. production vs staging might pin different cf_tunnel_name).