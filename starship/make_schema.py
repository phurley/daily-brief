# Development helper: regenerate the schema after contract edits.
import json
from pathlib import Path
S = {"type": "string"}
N = {"type": "null"}
T = {"type": "string", "format": "date-time"}
U = {"type": "string", "format": "uri", "pattern": "^https?://"}
def nullable(value): return {"anyOf": [value, N]}
def array(value): return {"type": "array", "items": value}
def obj(properties, optional=()): return {"type": "object", "additionalProperties": False, "required": [key for key in properties if key not in optional], "properties": properties}
def ref(key): return {"$ref": "#/$defs/" + key}
def enum(*values): return {"enum": list(values)}
target = obj(dict(precision=enum("unknown", "quarter", "month", "day", "window", "exact"), label={"type": "string", "minLength": 1}, lower=nullable(S), upper=nullable(S), net={"type": "boolean"}, timeZone=S))
evidence = obj(dict(id=S, sourceId=S, sourceUrl=U, sourceType=enum("operator", "regulator", "local-authority", "reporter", "editorial", "community"), publishedAt=nullable(T), observedAt=T, missionId=nullable(S), claimType=enum("target", "readiness", "regulatory", "notice", "delay", "scrub", "underway", "outcome", "discussion"), excerpt={"type": "string", "minLength": 1, "maxLength": 1000}, verification=enum("verified", "unverified", "retracted", "superseded"), claimConfidence=enum("confirmed", "reported"), originId=S, supersedes=array(S), parserVersion=S, target=ref("target"), authorization={"type": "boolean"}, noticeDate={"type": "string", "format": "date"}, outcome=enum("launched", "completed", "failed", "cancelled"), actualLiftoffAt=nullable(T), communityKind=enum("speculation", "linked-report", "discussion"), linkedSourceUrls=array(U)), ("communityKind", "linkedSourceUrls", "target", "authorization", "noticeDate", "outcome", "actualLiftoffAt"))
state = enum("unannounced", "targeted", "plausible", "delayed", "target-passed", "uncertain", "underway")
forecast = obj(dict(state=state, summary=S, window=nullable(ref("target")), basisIds=array(S), uncertainty=array(S)))
health = obj(dict(id=S, url=U, state=enum("ok", "stale", "no-dated-evidence", "error"), lastAttemptAt=T, lastSuccessAt=nullable(T), parserVersion=S, contentHash=nullable(S), detail=S))
root = obj(dict(schemaVersion={"const": "1.0.0"}, modelVersion=S, mode=enum("shadow", "live"), generatedAt=T, lastAttemptAt=T, lastSuccessAt=nullable(T), lastVerifiedAt=nullable(T), expiresAt=nullable(T), mission=obj(dict(id=nullable(S), label=S, vehicleIds=array(S))), status=state, statusEffectiveAt=T, officialTarget=nullable(obj(dict(evidenceId=S, sourceUrl=U, announcedAt=T, target=ref("target")))), outsideReports=array(S), forecast=forecast, evidence=array(ref("evidence")), conflicts=array(obj(dict(evidenceIds=array(S), explanation=S))), sourceHealth=array(health), outcomes=array(obj(dict(missionId=S, evidenceId=S, outcome=enum("launched", "completed", "failed", "cancelled"), actualLiftoffAt=nullable(T), recordedAt=T))), whatChanged=S, previousSnapshot=nullable(S), snapshot=nullable(S)))
estimate_source = obj(dict(id=S, url=U, title=S, excerpt={"type": "string", "minLength": 20, "maxLength": 900}, observedAt=T))
community_estimate = obj(dict(state=enum("estimated", "unavailable"), missionId=nullable(S), summary=S, rationale=S, windowStart=nullable({"type": "string", "format": "date"}), windowEnd=nullable({"type": "string", "format": "date"}), caveats=array(S), sources=array(estimate_source), generatedAt=T, expiresAt=T, model=S, version=S))
root["properties"]["communityEstimate"] = nullable(community_estimate)
root.update({"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Daily Brief Starship evidence and forecast", "$defs": {"target": target, "evidence": evidence}})
Path(__file__).resolve().parent.parent.joinpath("schemas/starship.schema.json").write_text(json.dumps(root, indent=2) + "\n")
