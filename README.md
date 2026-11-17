[![Discord](https://img.shields.io/badge/chat-on%20discord-7289da.svg?sanitize=true)](https://discord.gg/GUAy9wErNu)
[![](https://img.shields.io/discord/908084610158714900)](https://discord.gg/GUAy9wErNu)
[![Static Badge](https://img.shields.io/badge/github-assemblyline-blue?logo=github)](https://github.com/CybercentreCanada/assemblyline)
[![Static Badge](https://img.shields.io/badge/github-assemblyline\_service\_admarkup-blue?logo=github)](https://github.com/CybercentreCanada/assemblyline-service-admarkup)
[![GitHub Issues or Pull Requests by label](https://img.shields.io/github/issues/CybercentreCanada/assemblyline/service-admarkup)](https://github.com/CybercentreCanada/assemblyline/issues?q=is:issue+is:open+label:service-admarkup)
[![License](https://img.shields.io/github/license/CybercentreCanada/assemblyline-service-admarkup)](./LICENSE)
# AdMarkup Service

Identifies and extracts advertising markup from supported OpenRTB bid responses.

## Supported OpenRTB response subset

Recognition requires one JSON document with a root object containing a string `id`
and an array `seatbid`. The recognizer examines only direct
`seatbid[].bid[]` objects, and extracts a direct string `adm` only when that bid
also has string `id` and `impid` fields and a finite numeric `price`. Malformed
seats and bids are reported without preventing valid siblings from being used.
Empty and non-string `adm` values are reported and skipped. Duplicate object
keys and non-standard numeric constants make the whole JSON document invalid.
Missing `adm` is normal; markup may instead be delivered through `nurl`, which
this service does not fetch. Unknown optional fields are ignored.

Each accepted `adm` is encoded as UTF-8 without trimming or other content
transformation. Identical payload bytes are grouped in encounter order and
named `adm-<sha256>.txt`; their result includes every source JSON path, byte
length, and whether Assemblyline accepted them as extracted children. Each
unique payload is written in the service working directory and submitted with
`request.add_extracted`, so it is scanned in the same submission while the
original JSON remains available to other services. The `.txt` filename does
not force a file type: Assemblyline identifies child content, routing recognized
HTML and JavaScript to their corresponding types while leaving ordinary text
as text. Bid identifiers are not used in payload filenames.

## Resource limits

The service defaults to a 10 MiB input cap, JSON depth 64 and 100,000 JSON
nodes, 50 unique extracted children, 5 MiB per child, and 20 MiB total child
bytes. Byte limits use 1 MiB = 1,048,576 bytes. Depth starts at zero for the
root; node counts include containers and values but not object keys. Service
administrators can tune these limits in the manifest configuration. The
framework's per-request child count and platform extraction-depth and
file-size limits also apply. Inputs rejected by whole-document limits produce
no children; per-payload policy skips preserve valid siblings, and exhausted
aggregate/count budgets are reported as truncation.

This is a supported structural subset, not a full OpenRTB schema validator. It
does not verify request/response ID correlation or determine the exact protocol
minor version. Wrapped objects, arrays, recursive searches, and arbitrary keys
named `adm` are outside the contract.

The shape is based on the IAB [BidResponse, SeatBid, and Bid objects](https://github.com/InteractiveAdvertisingBureau/openrtb2.x/blob/1debba8001f40eb14597102dec3205afddf811d4/2.6.md#objectbidresponse)
and public RTBlint [video bid-response](https://rtblint.org/docs/openrtb-examples/video-bid-response/)
and [request/response-pair](https://rtblint.org/docs/openrtb-examples/request-response-pair/) examples.

## Input and testing contract

Automated tests must use synthetic data created specifically for the tests. Do not
read, copy, unpack, or execute supplied samples. No supplied-sample corpus is
included in this service directory.

## Base image and runtime

The Dockerfile pins `cccs/assemblyline-v4-service-base:4.7.0.stable5` to digest
`sha256:d01815ddbdb7df8cd5ef3e6dbbd5d8067cbdbbc0a702349629eb66b7171a16fc`
to match Assemblyline 4.7.0 deployments. Its image provides Python 3.11.14,
`assemblyline` 4.7.0.5 and `assemblyline-v4-service` 4.7.0.5, and
`requirements.txt` pins the same framework versions so `pip install` cannot
upgrade the base image's framework.

### Container smoke validation

The container was rebuilt on this base and checked on 2026-11-17 with Docker
29.4.0. The build used manifest version `4.7.0.dev1`; its local image ID was
`sha256:dab1348633ee229c7bbd1203fc71a8df24e358af11dc4259d9fc111242f75d6d`.
The runtime used the non-root `assemblyline` account (UID 1000). Framework
versions were Python 3.11.14, `assemblyline` 4.7.0.5, and
`assemblyline-v4-service` 4.7.0.5; the synthetic test run used pytest 9.2.0.

Run these commands from this service directory:

```sh
docker build --pull --build-arg version=4.7.0.dev1 \
  -t admarkup-service:run-0007 -f Dockerfile .

FULL_SAMPLES_LOCATION='' FULL_SELF_LOCATION='' docker run --rm --network bridge \
  -e FULL_SAMPLES_LOCATION -e FULL_SELF_LOCATION \
  -v "$PWD/tests:/opt/al_service/tests:ro" \
  --entrypoint bash admarkup-service:run-0007 \
  -c 'pip install --user -r tests/requirements.txt && pytest -p no:cacheprovider --durations=10 -rsx -vv -x'

docker run --rm --network none \
  -v "$PWD/tests/fixtures/openrtb_synthetic_response.json:/tmp/openrtb_synthetic_response.json:ro" \
  --entrypoint bash admarkup-service:run-0007 \
  -c 'python -m assemblyline_v4_service.dev.run_service_once admarkup.admarkup.AdMarkup /tmp/openrtb_synthetic_response.json'
```

The test command explicitly supplies the optional sample-location variables
as empty values; the generated `tests/pytest.sh` does not reference them. Its
test suite passed all 88 tests. The standalone command runs without network
access. Its Assemblyline result contained two extracted children with
`EXTRACTED` relations, expected names, hashes, exact bytes and source-path
provenance; the HTML child identified as `code/html`, and the short JavaScript
text identified as `text/plain`. The result score was zero, its result and
children had classification `TLP:C`, and all result sections were unscored.
A synthetic root-level `adm` decoy produced an unrecognized result with zero
children. The standalone runner copies extracted files into its output
directory's `working_directory` before finishing; the one-off validation
checked those copied bytes against each result entry's SHA-256. That standalone
check did not use a live Assemblyline instance.

### Live Assemblyline validation

On 2026-11-17 the service ran as `4.7.0.dev1` on a local Assemblyline 4.7 test
instance with only AdMarkup and DocumentPreview selected. Assemblyline disables
services whose version does not start with the system version (`4.7`), so
service versions such as `1.0.0` cannot be enabled. Eight benign synthetic
submissions (two positive, six negative) produced exactly eight top-level
submissions and no additional ones for children.

- Parents were identified as `text/json` and scored zero; children and results kept `TLP:C`.
- The mixed response produced three children with the expected SHA-256 values
  and source paths: `code/html`, `code/javascript` and `text/plain`. A response
  repeating one payload at three JSON paths produced one child with all three paths.
- The `code/html` child was scanned by DocumentPreview. No installed service
  accepted the `code/javascript` or `text/plain` children, so their downstream
  scanning remains unproven (for example, JsJaws was not installed).
- Truncated JSON (identified as `text/plain`, so not routed to AdMarkup), a
  root-level decoy, a request-style nested decoy, an array root, invalid or
  non-string bids and duplicate JSON keys produced zero children.

Reproduce with `live_validation/run_live_validation.py` from the workspace root.

## Image variants and tags

Assemblyline services are built from the [Assemblyline service base image](https://hub.docker.com/r/cccs/assemblyline-v4-service-base),
which is based on Debian with Python 3.11.

Assemblyline services use the following tag definitions:

| **Tag Type** | **Description**                                                                                  |      **Example Tag**       |
| :----------: | :----------------------------------------------------------------------------------------------- | :------------------------: |
|    latest    | The most recent build (can be unstable).                                                         |          `latest`          |
|  build_type  | The type of build used. `dev` is the latest unstable build. `stable` is the latest stable build. |     `stable` or `dev`      |
|    series    | Complete build details, including version and build type: `version.buildType`.                   | `4.5.stable`, `4.5.1.dev3` |

## Running this service

This is an Assemblyline service. It is designed to run as part of the Assemblyline framework.

If you would like to test this service locally, you can run the Docker image directly from the a shell:

    docker build -t cccs/assemblyline-service-admarkup:local .

To add this service to your Assemblyline deployment, follow this
[guide](https://cybercentrecanada.github.io/assemblyline4_docs/developer_manual/services/run_your_service/#add-the-container-to-your-deployment).

## Documentation

General Assemblyline documentation can be found at: https://cybercentrecanada.github.io/assemblyline4_docs/

