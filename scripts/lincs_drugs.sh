#!/usr/bin/env bash
set -euo pipefail

OUT_FILE="assets/lincs_all_molecule_names.txt"
CERT_DIR="${HOME}/.config/anduin-core/certs"
INTERMEDIATE_DER="${CERT_DIR}/InCommonRSAOVSSLCA3.crt"
INTERMEDIATE_PEM="${CERT_DIR}/InCommonRSAOVSSLCA3.pem"
CA_BUNDLE="${CERT_DIR}/curl-ca-bundle.pem"

mkdir -p "${CERT_DIR}" assets

# Work around a server-side chain issue: lincsportal presents a cert issued by
# InCommon RSA OV SSL CA 3 but does not provide that intermediate in-chain.
if [[ ! -s "${INTERMEDIATE_PEM}" ]]; then
  /usr/bin/curl --fail --show-error --silent \
    "http://crt.sectigo.com/InCommonRSAOVSSLCA3.crt" \
    -o "${INTERMEDIATE_DER}"
  openssl x509 -inform der -in "${INTERMEDIATE_DER}" -out "${INTERMEDIATE_PEM}"
fi

# Build local CA bundle from system trust + required intermediate.
cat /etc/ssl/certs/ca-certificates.crt "${INTERMEDIATE_PEM}" > "${CA_BUNDLE}"

tmp_out="$(mktemp)"
trap 'rm -f "${tmp_out}"' EXIT

for letter in {A..Z}; do
  /usr/bin/curl --fail --show-error --silent -G \
    --cacert "${CA_BUNDLE}" \
    --data-urlencode "searchTerm=Name:${letter}*" \
    --data-urlencode 'limit=10000' \
    "https://lincsportal.ccs.miami.edu/dcic/api/fetchmolecules" \
    -H "accept: application/json" \
  | jq -r '.results.documents[].Name' >> "${tmp_out}"
done

sort -u "${tmp_out}" > "${OUT_FILE}"
echo "Saved $(wc -l < "${OUT_FILE}") perturbagen names to ${OUT_FILE}"
