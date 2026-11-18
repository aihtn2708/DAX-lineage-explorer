"""Governance G3: tiers and the policies that hold each model to its tier."""
import threading
import time

import pytest

from dax_lineage.cli import main as daxlin
from dax_lineage.workspace import Store, scan_folders
from dax_lineage.workspace import auth as A
from dax_lineage.workspace import check as C
from dax_lineage.workspace import policy as P
from dax_lineage.workspace.app import AuthConfig, make_server
from dax_lineage.workspace.backup import backup, restore
from test_review import pbit, reviewed, save, the_save  # noqa: F401  (fixtures)
from test_workspace import call

PASSWORD = "correct horse battery"


def severities(store, version_id, rule_id):
    return {f["severity"] for f in C.version_check(store, version_id)["findings"] if f["rule_id"] == rule_id}


def test_every_model_starts_in_the_team_tier(reviewed):
    store, model, _, _ = reviewed
    assert P.tier_of(store, model["id"]) == "team" and store.models()[0]["tier"] == "team"
    verdict = P.evaluate_version(store, model["version_id"])
    assert verdict["tier"] == "team" and not verdict["ok"]
    by_id = {c["id"]: c for c in verdict["checks"]}
    assert not by_id["no_new_errors"]["ok"] and "Gross Margin %" in by_id["no_new_errors"]["detail"]
    assert by_id["max_score_drop"]["ok"] and "min_score" not in by_id
    [row] = P.compliance(store)
    assert not row["ok"] and row["problems"][0].startswith("A save may not add errors")


def test_personal_models_need_no_review(reviewed):
    store, model, _, _ = reviewed
    vid = model["version_id"]
    assert store.summary()["pending_reviews"] == 1
    P.set_tier(store, model["id"], "personal")
    assert store.version(vid)["review"] == "not_required"
    assert store.reviews("pending") == [] and store.summary()["pending_reviews"] == 0
    assert [r["review"] for r in store.reviews("all")] == ["not_required"]
    assert store.model(model["id"])["history"][0]["review"] == "not_required"
    verdict = P.evaluate_version(store, vid)
    assert verdict["checks"] == [] and verdict["ok"] and P.compliance(store)[0]["ok"]
    # a decision already taken stays
    P.set_tier(store, model["id"], "team")
    store.set_review(vid, "approved", by="An")
    P.set_tier(store, model["id"], "personal")
    assert store.version(vid)["review"] == "approved"


def test_certified_models_are_held_to_stricter_rules(reviewed):
    store, model, _, _ = reviewed
    vid = model["version_id"]
    assert severities(store, vid, "measure-description") == {"warning"}
    P.set_tier(store, model["id"], "certified")
    assert severities(store, vid, "measure-description") == {"error"}       # checked again at once
    assert not C.version_check(store, vid)["stale"]
    verdict = P.evaluate_version(store, vid)
    assert [c["id"] for c in verdict["checks"]] == ["no_new_errors", "max_score_drop", "min_score"]
    # the added measure has neither a description nor a format string: errors in this tier
    assert "Sales Target: no description" in verdict["checks"][0]["detail"]
    P.set_tier_settings(store, "certified", {"min_score": 95})
    assert not {c["id"]: c for c in P.evaluate_version(store, vid)["checks"]}["min_score"]["ok"]
    # rules set back to how everyone else has them
    P.set_tier_settings(store, "certified", {"rules": {}})
    assert severities(store, vid, "measure-description") == {"warning"}
    P.set_tier(store, model["id"], "team")
    assert P.reset_tier(store, "certified")["min_score"] == 80


def test_tier_settings_are_checked(reviewed):
    store, _, _, _ = reviewed
    for bad in ({"min_score": 101}, {"max_score_drop": "a lot"}, {"colour": "red"},
                {"rules": {"no-such-rule": {"severity": "error"}}}, {"rules": {"measure-format": {"severity": "fatal"}}},
                {"rules": ["measure-format"]}):
        with pytest.raises(ValueError):
            P.set_tier_settings(store, "team", bad)
    with pytest.raises(ValueError):
        P.set_tier_settings(store, "gold", {"min_score": 1})
    team = P.set_tier_settings(store, "team", {"max_score_drop": "", "require_review": 0,
                                               "rules": {"unused-measure": {"enabled": False}}}, by="Ada")
    assert team["max_score_drop"] is None and team["require_review"] is False and team["updated_by"] == "Ada"
    assert team["rules"] == {"unused-measure": {"enabled": False}}
    assert P.tiers(store)["certified"]["min_score"] == 80                     # other tiers untouched
    with pytest.raises(KeyError):
        P.set_tier(store, 999, "team")
    with pytest.raises(ValueError):
        P.set_tier(store, 1, "gold")


def test_a_clean_save_keeps_to_the_team_policy(tmp_path, pbit):
    store = Store(tmp_path / "ws.db")
    save(pbit, tmp_path / "Sales.pbit")
    scan_folders(store, [tmp_path])

    def describe(schema, layout):
        for table in schema["model"]["tables"]:
            for m in table.get("measures", []):
                if m["name"] == "Total Sales":
                    m["description"] = "Sales after discounts"
    save(pbit, tmp_path / "Sales.pbit", describe, bump=60)
    scan_folders(store, [tmp_path])
    model = store.models()[0]
    verdict = P.evaluate_version(store, model["version_id"])
    assert verdict["ok"] and all(c["ok"] for c in verdict["checks"])
    assert P.compliance(store)[0]["ok"]


def test_policies_api_and_roles(reviewed):
    store, model, _, _ = reviewed
    for name, role in (("rev", "reviewer"), ("ada", "admin")):
        A.add_account(store, name, role, name.title(), PASSWORD)
    httpd = make_server(store, port=0, quiet=True, auth=AuthConfig(mode="local"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port, vid = httpd.server_address[1], model["version_id"]

    def sign_in(name):
        _, _, response = call(port, "POST", "/api/login", {"username": name, "password": PASSWORD})
        return {"Cookie": response.getheader("Set-Cookie").split(";")[0]}

    rev, ada = sign_in("rev"), sign_in("ada")
    try:
        status, data, _ = call(port, "GET", "/api/policies", None, rev)
        assert status == 200 and data["order"] == ["personal", "team", "certified"] and data["models"][0]["tier"] == "team"
        assert {r["id"] for r in data["rules"]} >= {"measure-description", "metric-drift"}
        assert call(port, "GET", f"/api/models/{model['id']}", None, rev)[1]["policy"]["tier"] == "team"
        # only an admin sets tiers and policies
        assert call(port, "POST", f"/api/models/{model['id']}/tier", {"tier": "certified"}, rev)[0] == 403
        assert call(port, "POST", "/api/policies/team", {"min_score": 50}, rev)[0] == 403
        assert call(port, "POST", f"/api/models/{model['id']}/tier", {"tier": "gold"}, ada)[0] == 400
        assert call(port, "POST", "/api/policies/team", {"min_score": 500}, ada)[0] == 400
        assert call(port, "POST", "/api/policies/team", {"min_score": 50}, ada)[1]["min_score"] == 50
        assert call(port, "POST", "/api/policies/team", {"reset": True}, ada)[1]["min_score"] is None
        # a reviewer cannot approve a save that breaks the policy, even with a reason
        policy = call(port, "GET", f"/api/versions/{vid}", None, rev)[1]["policy"]
        assert not policy["ok"]
        status, refused, _ = call(port, "POST", f"/api/versions/{vid}/review",
                                  {"status": "approved", "override": "fine"}, rev)
        assert status == 409 and "An admin can approve it anyway" in refused["error"]
        status, refused, _ = call(port, "POST", f"/api/versions/{vid}/review", {"status": "approved"}, ada)
        assert status == 409 and "give a reason" in refused["error"]
        status, done, _ = call(port, "POST", f"/api/versions/{vid}/review",
                               {"status": "approved", "override": "The card is fixed in the next save"}, ada)
        assert status == 200 and store.version(vid)["review"] == "approved"
        assert store.comments(vid)[-1]["body"].startswith("Approved despite the team policy (A save may not add errors)")
        # once the model is personal, nothing waits
        assert call(port, "POST", f"/api/models/{model['id']}/tier", {"tier": "personal"}, ada)[1] == {"tier": "personal"}
        assert P.tier_of(store, model["id"]) == "personal"
        for _ in range(50):  # the audit row is written just after the answer is sent
            actions = [a["action"] for a in A.audit_log(store)]
            if "models.tier" in actions:
                break
            time.sleep(0.02)
        assert "models.tier" in actions and "policies.team" in actions
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_tiers_survive_backup_and_restore(reviewed, tmp_path):
    store, model, _, _ = reviewed
    P.set_tier(store, model["id"], "certified")
    P.set_tier_settings(store, "team", {"max_score_drop": 3})
    out = tmp_path / "ws.jsonl.gz"
    backup(store, out)
    copy = Store(tmp_path / "copy.db")
    restore(copy, out)
    assert P.tier_of(copy, model["id"]) == "certified" and P.tiers(copy)["team"]["max_score_drop"] == 3


def test_command_line(reviewed, capsys):
    store, model, _, _ = reviewed
    db = ["--db", str(store.path)]
    assert daxlin(["workspace", *db, "tier", "Sales", "certified"]) in (0, None)
    assert "Sales is now certified" in capsys.readouterr().out
    assert P.tier_of(store, model["id"]) == "certified"
    daxlin(["workspace", *db, "policies"])
    out = capsys.readouterr().out
    assert "certified  every save reviewed; no new errors" in out and "Sales" in out and "A save may not add errors" in out
    with pytest.raises(SystemExit):
        daxlin(["workspace", *db, "tier", "Nope", "team"])
