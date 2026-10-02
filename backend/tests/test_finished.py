"""Finished jobs: failure codes, run history, Fix and reschedule, reverting an abandoned fix, deleting jobs.

Design: Finished jobs and error messages; Fixing a job after a run; State rules; Deleting a job.
"""
from bmu.cspace import CSpaceError
from bmu.failures import catalog, classify, needs_fix
from bmu.storage import now


def new_job(api, name="Finished test"):
    return api.post("/api/jobs", json={"name": name}).json()["id"]


def run_once(api, job, worker):
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    assert worker.tick() is True
    return api.get(f"/api/jobs/{job}").json()


def by_file(j):
    return {r["file"]: r for r in j["rows"]}


def past_the_check(worker, monkeypatch):
    """Rows whose Media record exists while their Object step still has to search: the Object changed in the moment
    after the document's check, or the Media record was created before that check existed. Made here by leaving the
    Object out of the check, so the Object step finds the change itself."""
    monkeypatch.setattr(worker, "_check_object", lambda client, row: None)


# ---- the failure catalog ------------------------------------------------------------------------
def test_catalog_has_every_code_the_worker_records():
    codes = {"upload_too_large", "file_type_rejected", "file_missing", "media_rejected", "object_gone", "object_ambiguous",
             "object_rejected", "no_permission", "server_error", "duplicate_at_run", "group_failed", "auth",
             "account_inactive", "unavailable", "worker_stopped", "cancelled", "unknown"}
    assert codes <= set(catalog())
    assert needs_fix("upload_too_large") and needs_fix("object_gone") and not needs_fix("auth") and not needs_fix("cancelled")


def test_classify_maps_http_failures_to_catalog_codes():
    def e(status, code="unknown"):
        return CSpaceError(code, f"X returned {status}", status)
    assert classify("upload", e(413))[0] == "upload_too_large"
    assert classify("upload", e(415))[0] == "file_type_rejected"
    assert classify("media", e(400))[0] == "media_rejected"
    assert classify("createObject", e(400))[0] == "object_rejected"
    assert classify("relMediaObject", e(403, "forbidden"))[0] == "no_permission"
    assert classify("media", e(401, "auth"))[0] == "auth"
    assert classify("media", e(409))[0] == "account_inactive"
    assert classify("upload", e(503, "server"))[0] == "server_error"
    assert classify("upload", CSpaceError("unavailable", "timeout"))[0] == "server_error"
    code, detail = classify("relMediaObject", e(418))
    assert code == "unknown" and "418" in detail and "relMediaObject" in detail


def test_the_api_serves_the_catalog(api, login):
    login()
    f = api.get("/api/failures").json()["failures"]
    assert f["object_gone"]["title"] == "Object not found when the job ran" and f["object_gone"]["needs_fix"] is True


# ---- failures on demand in the simulated CollectionSpace ---------------------------------------
def test_failure_rules_affect_only_job_runs_by_default(api, login, add_uploaded, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    fail_on("objectSearch", effect="none", match="15-1234", count=0)
    # the editor's check still finds the object: the rule is for the worker only
    row = api.post(f"/api/jobs/{job}/check", json={"rows": [1]}).json()["rows"][0]
    assert not [c for c in row["checks"] if c["level"] == "block"]


# ---- a file rejected as too large: fix by replacing it -----------------------------------------
def test_upload_too_large_then_replace_file_and_rerun(api, login, add_uploaded, worker, services, fake, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "1-2345.jpg"])
    fail_on("upload", status=413, match="1-2345")
    j = run_once(api, job, worker)
    assert j["job"]["status"] == "NeedsAttention" and j["job"]["code"] == ""
    assert j["job"]["counts"] == {"done": 1, "partial": 1, "failed": 0, "notStarted": 0, "disabled": 0}
    row = by_file(j)["1-2345.jpg"]
    assert row["result"]["state"] == "Partial"
    assert row["result"]["error"]["code"] == "upload_too_large" and "413" in row["result"]["error"]["detail"]
    assert [r["outcome"] for r in j["runs"]] == ["NeedsAttention"] and j["runs"][0]["counts"]["partial"] == 1
    assert j["created"]["media"] == 2 and j["created"]["files"] == 1 and j["created"]["unfinished"] == 1
    old_key = row["s3Key"]

    # Only drafts can be scheduled: the job goes to Drafts first
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 409
    fixed = api.post(f"/api/jobs/{job}/fix").json()
    assert fixed["status"] == "Draft" and fixed["editingByYou"] and fixed["fixFrom"] == {"status": "NeedsAttention", "code": "", "codeDetail": "", "run": 1}
    # the Media record exists: its fields can't change, and it can't be deleted
    n = row["n"]
    r = api.patch(f"/api/jobs/{job}/rows/{n}", json={"description": "new"})
    assert r.status_code == 422 and "already exists in CollectionSpace" in r.json()["detail"]
    assert api.delete(f"/api/jobs/{job}/rows/{n}").status_code == 409
    chk = api.post(f"/api/jobs/{job}/check").json()["rows"]
    checks = next(x for x in chk if x["n"] == n)["checks"]
    assert any("The rerun will only upload the file" in c["text"] for c in checks)
    assert any(c["level"] == "warn" and "rejected this file" in c["text"] for c in checks)

    # a replacement file: the browser uploads it like any other
    r = api.post(f"/api/jobs/{job}/rows/{n}/replace-file", json={"name": "1-2345_small.jpg", "size": 5, "type": "image/jpeg"})
    assert r.status_code == 200, r.text
    new = r.json()["row"]
    assert new["upload"]["s"] == "pending" and new["s3Key"] != old_key and new["supersededKey"] == old_key
    services.storage.s3.put_object(Bucket=services.settings.s3_bucket, Key=new["s3Key"], Body=b"\xff\xd8\xffsm")
    after = api.post(f"/api/jobs/{job}/rows/{n}/uploaded").json()["row"]
    assert after["upload"]["s"] == "done" and not [c for c in after["checks"] if c["level"] != "info"]

    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    row = by_file(j)["1-2345_small.jpg"]
    assert j["job"]["status"] == "Completed" and j["job"]["run"] == 2 and j["job"]["expiresAt"] > now() + 29 * 86400
    assert row["result"]["state"] == "Done" and row["result"]["steps"]["media"]["run"] == 1 and row["result"]["steps"]["upload"]["run"] == 2
    blob = fake.blobs[row["result"]["steps"]["upload"]["csid"]]
    assert blob["name"] == "1-2345_small.jpg" and blob["size"] == 5
    assert services.storage.head_object(old_key) is None  # the rejected file was removed when the rerun started
    assert [r["outcome"] for r in j["runs"]] == ["NeedsAttention", "Completed"]
    assert services.storage.fix_originals(job) == []


# ---- the check before a document's records are created (design: Job execution) -----------------
def nothing_created(row, fake, media_before):
    st = row["result"]["steps"]
    return (row["result"]["state"] == "Failed" and st["values"]["s"] == "failed" and len(fake.media) == media_before
            and all(st[k]["s"] == "skipped" for k in st if k != "values") and st["media"] == {"s": "skipped", "after": "values"}
            and not fake.blobs and not fake.relations)


def test_an_object_that_went_missing_since_submitting_fails_the_document_with_nothing_created(api, login, add_uploaded, worker, fake, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    fail_on("objectSearch", effect="none", match="15-1234")
    media_before = len(fake.media)
    j = run_once(api, job, worker)
    row = j["rows"][0]
    assert row["result"]["error"] == {"code": "object_gone", "step": "values",
                                      "detail": 'GET collectionobjects?as=objectNumber = "15-1234" found 0 objects'}
    assert nothing_created(row, fake, media_before) and j["job"]["status"] == "NeedsAttention"
    assert j["created"] == {"media": 0, "files": 0, "objects": 0, "relations": 0, "groups": 0, "unfinished": 0}
    # nothing exists, so the document is as free to change as a draft's: another number, another handling, or deleting it
    api.post(f"/api/jobs/{job}/fix")
    assert api.patch(f"/api/jobs/{job}/rows/1", json={"handling": "mediaonly"}).status_code == 200
    assert api.patch(f"/api/jobs/{job}/rows/1", json={"handling": "link", "obj": "12-5678"}).status_code == 200
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "Completed" and j["rows"][0]["result"]["steps"]["values"]["s"] == "done"
    assert fake.objects[j["rows"][0]["result"]["steps"]["findObject"]["csid"]]["objectNumber"] == "12-5678"


def test_an_object_that_appeared_since_submitting_fails_a_create_only_document_with_nothing_created(api, login, add_uploaded, worker, fake):
    """The case that prompted the full check: "Create new object + link", and the Object was created in
    CollectionSpace between submitting and the run. No Media record is left without its Object."""
    login()
    job = new_job(api)
    add_uploaded(job, ["20-0901.jpg"])
    api.patch(f"/api/jobs/{job}/rows/1", json={"handling": "create"})
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    fake.objects["late"] = {"objectNumber": "20-0901", "deleted": False}
    media_before, objects_before = len(fake.media), len(fake.objects)
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    row = j["rows"][0]
    assert row["result"]["error"]["code"] == "object_exists" and row["result"]["error"]["step"] == "values"
    assert "found 1 object" in row["result"]["error"]["detail"]
    assert nothing_created(row, fake, media_before) and len(fake.objects) == objects_before
    api.post(f"/api/jobs/{job}/fix")
    checks = api.post(f"/api/jobs/{job}/check").json()["rows"][0]["checks"]
    assert any(c["level"] == "block" and "already exists" in c["text"] for c in checks), checks  # the editor says the same
    assert api.patch(f"/api/jobs/{job}/rows/1", json={"handling": "link"}).status_code == 200
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "Completed" and j["rows"][0]["result"]["steps"]["findObject"]["csid"] == "late"
    assert len(fake.media) == media_before + 1 and j["created"]["objects"] == 0


def test_an_object_number_that_now_matches_several_fails_the_document_with_nothing_created(api, login, add_uploaded, worker, fake, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    fail_on("objectSearch", effect="many", match="15-1234")
    media_before = len(fake.media)
    row = run_once(api, job, worker)["rows"][0]
    assert row["result"]["error"]["code"] == "object_ambiguous" and nothing_created(row, fake, media_before)


def test_a_permission_lost_since_submitting_fails_the_document_with_nothing_created(api, login, add_uploaded, worker, fake):
    """The account's permissions are read once per run; a document whose handling needs one it no longer has is
    failed by its check, in the editor's words."""
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "9-9999.jpg"])
    api.patch(f"/api/jobs/{job}/rows/2", json={"handling": "mediaonly"})
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    fake.perm_overrides["admin"] = {"relations": "RL"}
    media_before = len(fake.media)
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    linked, media_only = j["rows"]
    assert linked["result"]["error"]["code"] == "no_permission" and linked["result"]["error"]["step"] == "values"
    assert "can't create relations" in linked["result"]["error"]["detail"]
    assert linked["result"]["state"] == "Failed" and linked["result"]["steps"]["media"] == {"s": "skipped", "after": "values"}
    assert media_only["result"]["state"] == "Done" and len(fake.media) == media_before + 1  # it needs no relations


def test_link_or_create_without_create_on_objects_fails_only_when_the_object_is_missing(api, login, add_uploaded, worker, fake):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "20-0955.jpg"])
    for n in (1, 2):
        api.patch(f"/api/jobs/{job}/rows/{n}", json={"handling": "linkorcreate"})
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    fake.perm_overrides["admin"] = {"collectionobjects": "RL"}
    media_before = len(fake.media)
    worker.tick()
    exists, missing = api.get(f"/api/jobs/{job}").json()["rows"]
    assert exists["result"]["state"] == "Done" and exists["result"]["steps"]["findOrCreateObject"]["found"]
    assert missing["result"]["error"]["code"] == "no_permission" and "found 0 objects" in missing["result"]["error"]["detail"]
    assert missing["result"]["state"] == "Failed" and len(fake.media) == media_before + 1


def test_a_staged_file_that_is_gone_fails_the_document_with_nothing_created(api, login, add_uploaded, worker, fake, services):
    login()
    job = new_job(api)
    rows = add_uploaded(job, ["15-1234_1.jpg"])
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    s3, bucket = services.storage.s3, services.settings.s3_bucket
    for v in s3.list_object_versions(Bucket=bucket, Prefix=rows[0]["s3Key"])["Versions"]:  # every version: the bucket keeps them
        s3.delete_object(Bucket=bucket, Key=v["Key"], VersionId=v["VersionId"])
    media_before = len(fake.media)
    worker.tick()
    row = api.get(f"/api/jobs/{job}").json()["rows"][0]
    assert row["result"]["error"]["code"] == "file_missing" and nothing_created(row, fake, media_before)


def test_the_check_searches_for_the_object_once_and_the_object_step_reuses_it(api, login, add_uploaded, worker, monkeypatch):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    searches, real = [], worker.client_factory

    def counting(u, p):
        c = real(u, p)
        find = c.find_objects
        c.find_objects = lambda num: (searches.append(num), find(num))[1]
        return c
    worker.client_factory = counting
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "Completed" and searches == ["15-1234"]
    assert j["rows"][0]["result"]["steps"]["findObject"]["found"]


def test_if_permissions_cannot_be_read_the_run_goes_on_and_a_refusal_shows_at_its_step(api, login, add_uploaded, worker, fake):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    real = worker.client_factory

    def unreadable(u, p):
        from bmu.cspace import CSpaceError
        c = real(u, p)

        def boom():
            raise CSpaceError("server_error", "GET accounts/0/accountperms returned 500", status=500)
        c.account_permissions = boom
        return c
    worker.client_factory = unreadable
    worker.tick()
    assert api.get(f"/api/jobs/{job}").json()["job"]["status"] == "Completed"


# ---- past the check: correct the number or stop linking ----------------------------------------
def test_object_gone_then_stop_linking(api, login, add_uploaded, worker, fake, fail_on, monkeypatch):
    login()
    past_the_check(worker, monkeypatch)
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    fail_on("objectSearch", effect="none", match="15-1234")
    j = run_once(api, job, worker)
    row = j["rows"][0]
    assert row["result"]["state"] == "Partial" and row["result"]["error"]["code"] == "object_gone"
    assert api.post(f"/api/jobs/{job}/fix").status_code == 200
    # a wrong correction blocks; the object number may change because the object step failed on it
    r = api.patch(f"/api/jobs/{job}/rows/1", json={"obj": "20-0501"}).json()["row"]
    assert any(c["level"] == "block" and "No object 20-0501" in c["text"] for c in r["checks"])
    assert r["idnum"] == "15-1234"  # the Media record's identification number never follows
    r = api.patch(f"/api/jobs/{job}/rows/1", json={"skipLink": True}).json()["row"]
    assert not [c for c in r["checks"] if c["level"] == "block"]
    assert "stopped linking" in r["checks"][0]["text"]
    # once stopped, the object number is no longer what the rerun needs
    assert api.patch(f"/api/jobs/{job}/rows/1", json={"obj": "12-5678"}).status_code == 422
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    row = api.get(f"/api/jobs/{job}").json()["rows"][0]
    st = row["result"]["steps"]
    assert row["result"]["state"] == "Done"
    assert st["findObject"]["s"] == "not needed" and st["relMediaObject"]["s"] == "not needed"
    assert len(fake.relations) == 0


def test_object_ambiguous_at_run_and_correcting_the_number(api, login, add_uploaded, worker, fake, fail_on, monkeypatch):
    login()
    past_the_check(worker, monkeypatch)
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    fail_on("objectSearch", effect="many", match="15-1234")
    row = run_once(api, job, worker)["rows"][0]
    assert row["result"]["error"]["code"] == "object_ambiguous"
    api.post(f"/api/jobs/{job}/fix")
    r = api.patch(f"/api/jobs/{job}/rows/1", json={"obj": "12-5678"}).json()["row"]
    assert not [c for c in r["checks"] if c["level"] == "block"]
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    row = api.get(f"/api/jobs/{job}").json()["rows"][0]
    obj = row["result"]["steps"]["findObject"]["csid"]
    assert row["result"]["state"] == "Done" and fake.objects[obj]["objectNumber"] == "12-5678"


def test_no_permission_for_relations_offers_stop_linking(api, login, add_uploaded, worker, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    fail_on("relation", status=403, count=0)
    row = run_once(api, job, worker)["rows"][0]
    assert row["result"]["error"]["code"] == "no_permission" and row["result"]["state"] == "Partial"
    api.post(f"/api/jobs/{job}/fix")
    assert api.patch(f"/api/jobs/{job}/rows/1", json={"obj": "12-5678"}).status_code == 422  # the object was found
    assert api.patch(f"/api/jobs/{job}/rows/1", json={"skipLink": True}).status_code == 200


def test_media_rejected_row_is_editable_except_its_object(api, login, add_uploaded, worker, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["20-0777.jpg"])
    api.patch(f"/api/jobs/{job}/rows/1", json={"handling": "create"})
    fail_on("media", status=400)
    row = run_once(api, job, worker)["rows"][0]
    assert row["result"]["state"] == "Failed" and row["result"]["error"]["code"] == "media_rejected"
    assert row["result"]["steps"]["createObject"]["s"] == "done"
    api.post(f"/api/jobs/{job}/fix")
    assert api.patch(f"/api/jobs/{job}/rows/1", json={"description": "fixed"}).status_code == 200
    assert api.patch(f"/api/jobs/{job}/rows/1", json={"obj": "20-0778"}).status_code == 422
    # it created an Object, so it can't be deleted; the object isn't looked up again (it would now "already exist")
    assert api.delete(f"/api/jobs/{job}/rows/1").status_code == 409
    checks = api.post(f"/api/jobs/{job}/check").json()["rows"][0]["checks"]
    assert not [c for c in checks if c["level"] == "block"], checks
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "Completed" and j["created"]["objects"] == 1


def test_create_fails_when_the_object_appeared_after_the_check_and_the_row_can_switch_to_linking(api, login, add_uploaded, worker, fake, monkeypatch):
    """Design: "Create new object + link" creates a new object only; one that already exists fails the row
    (object_exists). Past the check the Media record exists, so the fix may switch the handling to one that links to
    that object (and only that)."""
    login()
    past_the_check(worker, monkeypatch)
    job = new_job(api)
    add_uploaded(job, ["20-0901.jpg"])
    api.patch(f"/api/jobs/{job}/rows/1", json={"handling": "create"})
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    fake.objects["late"] = {"objectNumber": "20-0901", "deleted": False}
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    row = j["rows"][0]
    st = row["result"]["steps"]["createObject"]
    assert st["s"] == "failed" and st["code"] == "object_exists" and "found 1 object" in st["detail"]
    assert row["result"]["state"] == "Partial" and j["created"]["objects"] == 0
    assert row["result"]["steps"]["relMediaObject"]["s"] == "skipped"
    assert j["job"]["status"] == "NeedsAttention"
    assert api.post(f"/api/jobs/{job}/fix").status_code == 200
    # still "create": the rerun check says the object exists
    checks = api.post(f"/api/jobs/{job}/check").json()["rows"][0]["checks"]
    assert any(c["level"] == "block" and "already exists" in c["text"] for c in checks), checks
    assert any(c["text"] == "The rerun will only create the object and link the Media record to the object." for c in checks), checks
    # only a handling that links to the existing object is allowed
    r = api.patch(f"/api/jobs/{job}/rows/1", json={"handling": "mediaonly"})
    assert r.status_code == 422 and "links to that object" in r.json()["detail"]
    assert api.patch(f"/api/jobs/{job}/rows/1", json={"handling": "link"}).status_code == 200
    checks = api.post(f"/api/jobs/{job}/check").json()["rows"][0]["checks"]
    assert not [c for c in checks if c["level"] == "block"], checks
    assert any("find the object" in c["text"] for c in checks), checks
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    steps = j["rows"][0]["result"]["steps"]
    assert j["job"]["status"] == "Completed" and steps["findObject"]["csid"] == "late" and steps["findObject"]["found"]
    assert steps["createObject"]["s"] == "not needed" and j["created"]["objects"] == 0


def test_link_or_create_links_an_object_that_appeared_and_creates_a_missing_one(api, login, add_uploaded, worker, fake):
    """Design: "Link to object (create if missing)" links to the object if one exists, and creates it otherwise."""
    login()
    job = new_job(api)
    add_uploaded(job, ["20-0902.jpg", "20-0903.jpg"])
    for n in (1, 2):
        api.patch(f"/api/jobs/{job}/rows/{n}", json={"handling": "linkorcreate"})
    rows = api.post(f"/api/jobs/{job}/check").json()["rows"]
    assert not [c for r in rows for c in r["checks"] if c["level"] == "block"]
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    fake.objects["late2"] = {"objectNumber": "20-0902", "deleted": False}
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    by = {r["file"]: r["result"]["steps"]["findOrCreateObject"] for r in j["rows"]}
    assert by["20-0902.jpg"]["csid"] == "late2" and by["20-0902.jpg"]["found"] is True
    assert by["20-0903.jpg"]["s"] == "done" and not by["20-0903.jpg"].get("found")
    assert j["job"]["status"] == "Completed" and j["created"]["objects"] == 1


# ---- job-level failures -------------------------------------------------------------------------
def test_sign_in_failure_fails_the_job_and_reschedule_continues(api, login, add_uploaded, worker, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "12-5678_1.jpg"])
    fail_on("media", status=401)
    j = run_once(api, job, worker)
    assert j["job"]["status"] == "Failed" and j["job"]["code"] == "auth"
    assert j["runs"][0]["code"] == "auth" and j["job"]["counts"]["notStarted"] == 1
    # the technical detail shown on request: where it happened and the HTTP status (design: Finished jobs)
    assert j["job"]["codeDetail"] == "Document 1, step media: POST media returned 401"
    assert j["runs"][0]["codeDetail"] == j["job"]["codeDetail"]
    fixed = api.post(f"/api/jobs/{job}/fix").json()
    assert fixed["fixFrom"]["code"] == "auth" and fixed["fixFrom"]["codeDetail"] == j["job"]["codeDetail"]
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "Completed" and j["job"]["code"] == "" and j["job"]["codeDetail"] == ""
    assert j["runs"][1]["codeDetail"] == ""


def test_five_failed_requests_in_a_row_stop_the_job(api, login, add_uploaded, worker, fail_on):
    """Design: five consecutive 5xx or network failures stop the job as "unavailable". They are counted per
    request: here each media-only document makes two failing requests (Media ID search, create Media)."""
    login()
    job = new_job(api)
    add_uploaded(job, [f"15-1234_{i}.jpg" for i in range(1, 6)])
    for n in range(1, 6):
        api.patch(f"/api/jobs/{job}/rows/{n}", json={"handling": "mediaonly"})
    for step in ("media", "mediaSearch"):
        fail_on(step, status=503, count=0)
    j = run_once(api, job, worker)
    assert j["job"]["status"] == "Failed" and j["job"]["code"] == "unavailable"
    assert j["job"]["counts"]["failed"] == 3 and j["job"]["counts"]["notStarted"] == 2
    assert j["job"]["codeDetail"].startswith("5 requests in a row to CollectionSpace failed, the last: GET media")
    assert "stopped at document 3, step media" in j["job"]["codeDetail"]
    assert [(r["result"] or {}).get("state", "Not started") for r in j["rows"]][:3] == ["Failed", "Failed", "Failed"]


def test_failures_between_successful_requests_do_not_stop_the_job(api, login, add_uploaded, worker, fail_on):
    """A request that fails between ones that succeed is a one-off server error, not an outage."""
    login()
    job = new_job(api)
    add_uploaded(job, [f"15-1234_{i}.jpg" for i in range(1, 7)])
    fail_on("media", status=503, count=0)
    j = run_once(api, job, worker)
    assert j["job"]["status"] == "NeedsAttention" and j["job"]["counts"]["failed"] == 6


def test_a_running_job_whose_worker_stopped_fails_as_worker_stopped(api, login, add_uploaded, worker, services):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    # as if a worker had claimed it, started the row and then died
    row = services.storage.get_row(job, 1)
    row["result"] = {"state": "In progress", "steps": {"media": {"s": "done", "csid": "m1", "run": 1}}, "run": 1}
    services.storage.put_row(job, row, guard=False)
    services.storage.put_run(job, {"run": 1, "startedAt": now() - 900, "outcome": "Running"})
    services.storage.update_job(job, {"status": "Running", "run": 1, "startedAt": now() - 900, "heartbeatAt": now() - 600})
    assert worker.tick() is False  # a Running job is never resumed; the periodic check stops it instead
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "Failed" and j["job"]["code"] == "worker_stopped"
    assert j["rows"][0]["result"]["state"] == "Partial"
    assert j["runs"][0]["outcome"] == "Failed" and j["runs"][0]["code"] == "worker_stopped"
    assert j["job"]["codeDetail"] == "No heartbeat for 10 minutes" == j["runs"][0]["codeDetail"]
    assert services.storage.get_credential(job) is None
    # the row the worker was on can't be deleted: a create may have reached CollectionSpace
    api.post(f"/api/jobs/{job}/fix")
    assert api.delete(f"/api/jobs/{job}/rows/1").status_code == 409


def test_a_row_the_worker_stopped_on_before_recording_a_csid_stays_undeletable(api, login, add_uploaded, worker, services):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    # the worker died during "create the Media record": nothing recorded, but the create may have reached CollectionSpace
    row = services.storage.get_row(job, 1)
    row["result"] = {"state": "In progress", "steps": {}, "run": 1}
    services.storage.put_row(job, row, guard=False)
    services.storage.put_run(job, {"run": 1, "startedAt": now() - 900, "outcome": "Running"})
    services.storage.update_job(job, {"status": "Running", "run": 1, "startedAt": now() - 900, "heartbeatAt": now() - 600})
    worker.tick()
    res = api.get(f"/api/jobs/{job}").json()["rows"][0]["result"]
    assert res["state"] == "Not started" and res["interrupted"] == 1
    api.post(f"/api/jobs/{job}/fix")
    r = api.delete(f"/api/jobs/{job}/rows/1")
    assert r.status_code == 409 and "can't be deleted" in r.json()["detail"]
    # a later run that finishes the row clears the mark
    api.post(f"/api/jobs/{job}/schedule")
    worker.tick()
    assert "interrupted" not in services.storage.get_row(job, 1)["result"]


def test_cancelled_run_is_recorded_in_the_run_history(api, login, add_uploaded, worker, services):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "12-5678_1.jpg"])
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    original = worker.run_row

    def cancel_after_first(client, job_id, row, run_no, created):
        services.storage.update_job(job_id, {"cancelRequested": {"by": "admin", "at": now()}})
        return original(client, job_id, row, run_no, created)
    worker.run_row = cancel_after_first
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["code"] == "cancelled" and j["runs"][0]["cancelledBy"] == "admin"
    assert j["job"]["codeDetail"].startswith("Cancel requested by admin, ") and j["job"]["codeDetail"].endswith(" Pacific time")
    assert j["job"]["counts"] == {"done": 1, "partial": 0, "failed": 0, "notStarted": 1, "disabled": 0}


def test_duplicate_id_created_after_scheduling_is_a_notice(api, login, add_uploaded, worker, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["12-5678_1.jpg"])
    fail_on("mediaSearch", effect="many")
    row = run_once(api, job, worker)["rows"][0]
    assert row["result"]["state"] == "Done"
    assert row["result"]["notices"][0]["code"] == "duplicate_at_run"


# ---- the run history lists what was disabled or deleted before each run ------------------------
def test_run_history_lists_documents_disabled_or_deleted_before_the_run(api, login, add_uploaded, worker, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "12-5678_1.jpg", "1-2345.jpg"])
    fail_on("media", status=400, match="12-5678")
    fail_on("media", status=500, match="1-2345")
    run_once(api, job, worker)
    api.post(f"/api/jobs/{job}/fix")
    r = api.patch(f"/api/jobs/{job}/rows/2", json={"include": False}).json()["row"]
    assert r["disabledBy"] == "admin" and r["disabledAt"]
    assert api.post(f"/api/jobs/{job}/save").json()["status"] == "Draft"  # document 3 still has work left
    # deleting the last unfinished document (a Failed one that created nothing) leaves every document done or
    # disabled: the job is Completed
    r = api.delete(f"/api/jobs/{job}/rows/3")
    assert r.status_code == 200 and r.json()["jobStatus"] == "Completed"
    done = api.get(f"/api/jobs/{job}").json()["job"]
    assert done["status"] == "Completed" and not done.get("editingBy") and done["expiresAt"] > now() + 29 * 86400
    assert done["deletedRows"][0]["file"] == "1-2345.jpg"


def test_rerun_records_disabled_documents(api, login, add_uploaded, worker, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "12-5678_1.jpg", "1-2345.jpg"])
    fail_on("media", status=400, match="12-5678")
    fail_on("media", status=500, match="1-2345")
    run_once(api, job, worker)
    api.post(f"/api/jobs/{job}/fix")
    api.patch(f"/api/jobs/{job}/rows/2", json={"include": False})
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "Completed" and j["job"]["counts"]["disabled"] == 1
    run2 = j["runs"][1]
    assert run2["scheduledBy"] == "admin" and [d["file"] for d in run2["disabledBefore"]] == ["12-5678_1.jpg"]


# ---- an abandoned fix is reverted -------------------------------------------------------------
def test_an_abandoned_fix_is_reverted(api, login, add_uploaded, worker, services, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "12-5678_1.jpg"])
    fail_on("media", status=400, match="12-5678")
    fail_on("upload", status=413, match="15-1234")
    run_once(api, job, worker)
    api.post(f"/api/jobs/{job}/fix")
    api.patch(f"/api/jobs/{job}/rows/2", json={"description": "changed while fixing"})
    r = api.post(f"/api/jobs/{job}/rows/1/replace-file", json={"name": "15-1234_1s.jpg", "size": 3, "type": "image/jpeg"}).json()
    new_key, old_key = r["row"]["s3Key"], r["row"]["supersededKey"]
    services.storage.s3.put_object(Bucket=services.settings.s3_bucket, Key=new_key, Body=b"abc")
    add_uploaded(job, ["1-2345.jpg"])
    assert len(api.get(f"/api/jobs/{job}").json()["rows"]) == 3
    services.storage.update_job(job, {"draftExpiresAt": now() - 1})  # a fix's expiry (design: State rules)
    assert worker.sweep_expired_drafts() == [job]
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "NeedsAttention" and j["job"]["fixFrom"] is None and not j["job"].get("editingBy")
    rows = by_file(j)
    assert set(rows) == {"15-1234_1.jpg", "12-5678_1.jpg"}
    assert rows["12-5678_1.jpg"]["description"] == "" and rows["15-1234_1.jpg"]["s3Key"] == old_key
    assert services.storage.head_object(new_key) is None and services.storage.head_object(old_key) is not None
    assert len(j["runs"]) == 1
    assert any(a["type"] == "Fix reverted" for a in services.storage.list_audit("pahma"))


def test_drafts_that_never_ran_are_still_deleted_on_expiry(api, login, add_uploaded, worker, services):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    services.storage.update_job(job, {"expiresAt": now() - 1})
    assert worker.sweep_expired_drafts() == [job]
    assert services.storage.get_job(job) is None


def test_completed_jobs_are_removed_after_30_days(api, login, add_uploaded, worker, services):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    assert run_once(api, job, worker)["job"]["status"] == "Completed"
    assert worker.sweep_completed() == []
    services.storage.update_job(job, {"expiresAt": now() - 1})
    assert worker.sweep_completed() == [job]
    assert services.storage.get_job(job) is None
    assert any(a["type"] == "Job expired" for a in services.storage.list_audit("pahma"))


# ---- deleting a job that created records ------------------------------------------------------
def test_a_job_that_created_records_can_be_deleted_and_the_audit_keeps_its_csids(api, login, add_uploaded, worker, services, fake, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "12-5678_1.jpg"])
    fail_on("upload", status=413, match="12-5678")
    j = run_once(api, job, worker)
    assert j["job"]["status"] == "NeedsAttention"
    assert api.delete(f"/api/jobs/{job}").status_code == 200
    assert services.storage.get_job(job) is None and services.storage.get_runs(job) == []
    entry = next(a for a in services.storage.list_audit("pahma") if a["type"] == "Job deleted")
    assert "2 Media records (1 with files)" in entry["detail"] and "1 unfinished" in entry["detail"]
    assert {c["step"] for c in entry["csids"]} == {"media", "upload", "relMediaObject", "relObjectMedia"}
    assert len(fake.media) == 3  # nothing in CollectionSpace was touched


def test_running_and_completed_jobs_cannot_be_deleted(api, login, add_uploaded, worker, services):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    run_once(api, job, worker)
    assert api.delete(f"/api/jobs/{job}").status_code == 409  # Completed: removed on its own
    services.storage.update_job(job, {"status": "Running"})
    assert api.delete(f"/api/jobs/{job}").status_code == 409


def test_only_one_person_fixes_a_job(api, login, add_uploaded, worker, services, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["12-5678_1.jpg"])
    fail_on("media", status=400)
    run_once(api, job, worker)
    assert api.post(f"/api/jobs/{job}/fix").status_code == 200
    r = api.post(f"/api/jobs/{job}/fix")
    assert r.status_code == 409 and "already fixing" in r.json()["detail"]


def test_ids_shared_within_the_job_are_not_reported_as_new_duplicates(api, login, add_uploaded, worker):
    login()
    job = new_job(api)
    add_uploaded(job, ["12-5678_1.jpg", "12-5678_2.jpg"])  # the same identification number, warned in the editor
    rows = run_once(api, job, worker)["rows"]
    assert all(not r["result"].get("notices") for r in rows)


def test_a_file_whose_content_does_not_match_its_name_is_rejected_before_upload(api, login, add_uploaded, worker, fake):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.tif"], content=b"\xff\xd8\xff\xe0 really a JPEG")
    media_before = len(fake.media)
    row = run_once(api, job, worker)["rows"][0]
    st = row["result"]["steps"]["values"]  # the document's check, before its Media record is created
    assert st["code"] == "file_type_rejected" and "content is JPEG" in st["detail"]
    assert row["result"]["state"] == "Failed" and not fake.blobs and len(fake.media) == media_before  # nothing was sent


def test_the_audit_log_keeps_per_row_detail_and_a_csid_index(api, login, add_uploaded, worker, services, fail_on, monkeypatch):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "12-5678_1.jpg"])
    fail_on("upload", status=413, match="12-5678")
    j = run_once(api, job, worker)
    entry = next(a for a in services.storage.list_audit("pahma") if a["type"] == "Run")
    rows = {r["file"]: r for r in entry["rows"]}
    assert rows["12-5678_1.jpg"]["errors"] == ["upload_too_large"] and rows["12-5678_1.jpg"]["obj"] == "12-5678"
    assert rows["15-1234_1.jpg"]["state"] == "Done" and set(rows["15-1234_1.jpg"]["csids"]) >= {"media", "upload"}
    media = j["rows"][0]["result"]["steps"]["media"]["csid"]
    found = services.storage.find_csid(media)
    assert found["job"] == job and found["recordType"] == "Media" and found["run"] == 1 and found["row"] == 1
    assert services.storage.find_csid(j["rows"][0]["result"]["steps"]["findObject"]["csid"]) is None  # found, not created
    # a large run's detail goes to S3
    import bmu.worker as w
    monkeypatch.setattr(w, "INLINE_AUDIT_ROWS", 1)
    api.post(f"/api/jobs/{job}/fix")
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    entry = next(a for a in services.storage.list_audit("pahma") if a["type"] == "Run" and a.get("run") == 2)
    assert "rows" not in entry and entry["detailKey"].startswith(f"audit/pahma/{job}/run-002")
    import json
    assert len(json.loads(services.storage.get_bytes(entry["detailKey"]))) == 2


def test_periodic_checks_also_run_between_documents_of_a_running_job(api, login, add_uploaded, worker, services):
    """A long run must not hold up the periodic checks (draft expiry, cleanup, stalled jobs)."""
    login()
    stale = new_job(api, "never scheduled")
    services.storage.update_job(stale, {"expiresAt": 1})  # expired long ago
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "15-1234_2.jpg"])
    seen = []
    real_sweep, real_row = worker.sweep, worker.run_row

    def sweep():
        seen.append((api.get(f"/api/jobs/{job}").json()["job"]["status"]))
        real_sweep()

    def run_row(*a, **kw):
        out = real_row(*a, **kw)
        worker._last_sweep = 0  # "a minute later"
        return out
    worker.sweep, worker.run_row = sweep, run_row
    worker._last_sweep = 10 ** 12  # nothing due before the run starts
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    assert "Running" in seen
    assert services.storage.get_job(stale) is None
    assert api.get(f"/api/jobs/{job}").json()["job"]["status"] == "Completed"



def test_only_a_success_resets_the_failed_request_count(fake):
    import pytest
    from bmu.cspace import CSpaceError
    from conftest import factory
    c = factory("admin", "admin")
    c.failures_in_a_row = 3
    with pytest.raises(CSpaceError):
        c._request("GET", "media/no-such-csid")  # a 404: an answer about one record, not an outage
    assert c.failures_in_a_row == 3
    c.find_objects("15-1234")
    assert c.failures_in_a_row == 0



def test_deleting_a_queued_job_first_takes_it_out_of_the_workers_reach(api, login, add_uploaded, worker, services, monkeypatch):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    # the worker claims the job between the web app's read and its delete: the delete must not go ahead
    real = services.storage.begin_delete
    def claimed_first(*a, **k):
        assert services.storage.claim_job(job, {"status": "Running"})
        return real(*a, **k)
    monkeypatch.setattr(services.storage, "begin_delete", claimed_first)
    assert api.delete(f"/api/jobs/{job}").status_code == 409
    assert services.storage.get_job(job)["status"] == "Running" and services.storage.get_rows(job)
    monkeypatch.undo()
    # the other way round: once deletion has begun, the worker can't claim it
    job2 = new_job(api)
    add_uploaded(job2, ["1-2345_1.jpg"])
    api.post(f"/api/jobs/{job2}/schedule")
    assert services.storage.begin_delete(job2, ["Queued"], "any")
    assert services.storage.get_credential(job2) is None
    assert not services.storage.claim_job(job2, {"status": "Running"})


def test_a_deletion_that_stopped_part_way_is_finished_by_the_sweep(api, login, add_uploaded, worker, services):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    services.storage.update_job(job, {"status": "Deleting", "deletingSince": now() - 700})
    assert worker.sweep_unfinished_deletions() == [job] and services.storage.get_job(job) is None


# ---- temporary states are finished by the sweep after ten minutes -------------------------------------
def test_a_job_left_stopping_is_finished_as_worker_stopped(api, login, add_uploaded, worker, services):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    row = services.storage.get_row(job, 1)
    row["result"] = {"state": "In progress", "steps": {"media": {"s": "done", "csid": "m1", "run": 1}}, "run": 1}
    services.storage.put_row(job, row, guard=False)
    services.storage.put_run(job, {"run": 1, "startedAt": now() - 900, "outcome": "Running"})
    # the sweep that stopped it was itself interrupted right after setting Stopping
    services.storage.update_job(job, {"status": "Stopping", "stoppingSince": now() - 60, "run": 1, "scheduledBy": "admin"})
    assert worker.sweep_interrupted_transitions() == []  # not yet: it may still be finishing
    services.storage.update_job(job, {"stoppingSince": now() - 700})
    worker.sweep()
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "Failed" and j["job"]["code"] == "worker_stopped"
    assert j["rows"][0]["result"]["state"] == "Partial" and j["runs"][0]["outcome"] == "Failed"
    assert j["job"]["codeDetail"] == "No heartbeat"  # the interrupted sweep's own detail wasn't stored
    assert services.storage.get_credential(job) is None
    assert worker.sweep_interrupted_transitions() == []  # done once


def test_a_job_left_reverting_is_reverted_again(api, login, add_uploaded, worker, services, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "12-5678_1.jpg"])
    fail_on("media", status=400, match="12-5678")
    run_once(api, job, worker)
    api.post(f"/api/jobs/{job}/fix")
    api.patch(f"/api/jobs/{job}/rows/2", json={"description": "changed while fixing"})
    add_uploaded(job, ["1-2345.jpg"])
    # the revert was interrupted part way: one added document already removed, the rest not done
    services.storage.update_job(job, {"status": "Reverting", "revertingSince": now() - 700})
    services.storage.delete_row(job, 3)
    assert worker.sweep_interrupted_transitions() == [job]
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "NeedsAttention" and j["job"]["fixFrom"] is None and not j["job"].get("editingBy")
    assert set(by_file(j)) == {"15-1234_1.jpg", "12-5678_1.jpg"} and by_file(j)["12-5678_1.jpg"]["description"] == ""
    assert services.storage.fix_originals(job) == []
    assert [a["type"] for a in services.storage.list_audit("pahma")].count("Fix reverted") == 1


def test_an_expiry_left_part_way_is_finished_with_one_audit_entry(api, login, add_uploaded, worker, services):
    login()
    done = new_job(api, "done long ago")
    add_uploaded(done, ["12-5678_1.jpg"])
    assert run_once(api, done, worker)["job"]["status"] == "Completed"
    draft = new_job(api, "old draft")
    add_uploaded(draft, ["15-1234_1.jpg"])
    services.storage.update_job(draft, {"status": "Expiring", "expiringFrom": "Draft", "expiringSince": now() - 700})
    # interrupted after its audit entry was written
    services.storage.update_job(done, {"status": "Expiring", "expiringFrom": "Completed", "expiringSince": now() - 700})
    assert services.storage.audit_once("pahma", done, "Expiring", "expiryAudited", "Job expired", "BMU", "Removed …")
    assert sorted(worker.sweep_interrupted_transitions()) == sorted([draft, done])
    assert services.storage.get_job(draft) is None and services.storage.get_rows(draft) == []
    assert services.storage.get_job(done) is None and services.storage.get_runs(done) == []
    types = [a["type"] for a in services.storage.list_audit("pahma")]
    assert types.count("Draft expired") == 1 and types.count("Job expired") == 1


# ---- deleting a job: the complete audit entry comes first -------------------------------------------
def test_a_job_deletion_writes_its_audit_entry_before_deleting_anything(api, login, add_uploaded, worker, services, fail_on, monkeypatch):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "12-5678_1.jpg"])
    fail_on("upload", status=413, match="12-5678")
    run_once(api, job, worker)

    def crash(job_id):
        raise RuntimeError("web app stopped")
    monkeypatch.setattr(services.storage, "delete_job_and_files", crash)
    import pytest
    with pytest.raises(RuntimeError):
        api.delete(f"/api/jobs/{job}")
    monkeypatch.undo()
    entry = next(a for a in services.storage.list_audit("pahma") if a["type"] == "Job deleted")
    assert entry["user"] == "admin" and entry["jobName"] == "Finished test" and entry["counts"]["media"] == 2
    assert {c["step"] for c in entry["csids"]} == {"media", "upload", "relMediaObject", "relObjectMedia"}
    j = services.storage.get_job(job)
    assert j["status"] == "Deleting" and j["deletedBy"] == "admin" and j["deleteAudited"] is True
    services.storage.update_job(job, {"deletingSince": now() - 700})
    assert worker.sweep_unfinished_deletions() == [job]
    assert services.storage.get_job(job) is None and services.storage.get_rows(job) == []
    assert [a["type"] for a in services.storage.list_audit("pahma")].count("Job deleted") == 1


def test_a_deletion_stopped_before_its_audit_entry_is_audited_by_the_sweep_for_the_user(api, login, add_uploaded, worker, services, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    fail_on("upload", status=413)
    run_once(api, job, worker)
    assert services.storage.begin_delete(job, ["NeedsAttention"], "a-session", "admin")  # then the web app stopped
    services.storage.update_job(job, {"deletingSince": now() - 700})
    assert worker.sweep_unfinished_deletions() == [job] and services.storage.get_job(job) is None
    entries = [a for a in services.storage.list_audit("pahma") if a["type"] == "Job deleted"]
    assert len(entries) == 1 and entries[0]["user"] == "admin"
    assert {c["step"] for c in entries[0]["csids"]} == {"media", "relMediaObject", "relObjectMedia"}
    assert "finished the deletion" in entries[0]["detail"]


def test_the_deletion_audit_entry_is_written_once_even_if_the_sweep_overlaps(api, login, services):
    login()
    job = new_job(api)
    assert services.storage.begin_delete(job, ["Draft"], services.storage.get_job(job)["editingSession"], "admin")
    created = {"counts": {}, "csids": []}
    assert services.storage.audit_job_deleted("pahma", job, "admin", "web app", created) is True
    assert services.storage.audit_job_deleted("pahma", job, "admin", "sweep", created) is False
    assert [a["detail"] for a in services.storage.list_audit("pahma") if a["type"] == "Job deleted"] == ["web app"]


# ---- five failed requests in a row: nothing is sent after the fifth -----------------------------------
def test_no_request_is_sent_after_the_fifth_failure_in_a_row(api, login, add_uploaded, worker, fake, fail_on):
    """The duplicate-ID search fails as the fifth failure in a row: the create in the same step must not follow."""
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    fail_on("mediaSearch", status=503, count=0)
    real = worker.client_factory

    def four_failed_already(u, p):
        c = real(u, p)
        find_objects = c.find_objects

        def then_four_failures(num):  # the document's check finds its object: then four failures
            out = find_objects(num)
            c.failures_in_a_row = 4
            return out
        c.find_objects = then_four_failures
        return c
    worker.client_factory = four_failed_already
    media_before = len(fake.media)
    j = run_once(api, job, worker)
    assert j["job"]["status"] == "Failed" and j["job"]["code"] == "unavailable"
    assert len(fake.media) == media_before  # create_media was never sent
    assert j["rows"][0]["result"]["steps"]["media"]["s"] == "not run"


def test_the_client_refuses_to_send_once_the_limit_is_reached(fake):
    import pytest
    from bmu.cspace import CSpaceError, CSpaceUnavailable
    from conftest import factory
    c = factory("admin", "admin")
    c.failures_in_a_row = 5
    assert c.find_objects("15-1234")  # the web app's clients have no limit
    c.failures_in_a_row, c.max_failures_in_a_row = 5, 5
    sent = []
    real = c._http.request
    c._http.request = lambda *a, **k: sent.append(a) or real(*a, **k)
    with pytest.raises(CSpaceUnavailable) as e:
        c.find_objects("15-1234")
    assert isinstance(e.value, CSpaceError) and e.value.code == "unavailable" and sent == []


def test_the_group_step_stops_the_job_when_the_client_refuses_to_send(api, login, add_uploaded, worker, fake, monkeypatch):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    api.patch(f"/api/jobs/{job}", json={"groupOn": True, "groupTitle": "Survey batch 4"})
    from bmu.cspace import CSpaceClient

    def unavailable(self, xml):
        self.failures_in_a_row = 5
        return CSpaceClient._request(self, "POST", "groups")
    monkeypatch.setattr(CSpaceClient, "create_group", unavailable)
    j = run_once(api, job, worker)
    assert j["job"]["status"] == "Failed" and j["job"]["code"] == "unavailable"
    assert not j["job"].get("groupStep")  # not recorded as a failed Group: nothing was sent
    assert not fake.groups


# ---- a sign-in failure while creating the Group settles the row -----------------------------------------
def test_a_sign_in_failure_creating_the_group_fails_the_step_not_the_row(api, login, add_uploaded, worker, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "12-5678_1.jpg"])
    api.patch(f"/api/jobs/{job}", json={"groupOn": True, "groupTitle": "Survey batch 4"})
    fail_on("group", status=401)
    j = run_once(api, job, worker)
    assert j["job"]["status"] == "Failed" and j["job"]["code"] == "auth"
    first = j["rows"][0]["result"]
    assert first["state"] == "Partial" and "interrupted" not in first  # CollectionSpace refused it: nothing was created
    assert first["steps"]["addToGroup"]["s"] == "failed" and first["steps"]["addToGroup"]["code"] == "auth"
    assert first["error"]["code"] == "auth"
    assert j["rows"][1]["result"] is None  # not reached
    api.post(f"/api/jobs/{job}/fix")
    assert api.delete(f"/api/jobs/{job}/rows/2").status_code == 200


# ---- housekeeping --------------------------------------------------------------------------------------
def test_deleted_rows_move_to_the_run_item_when_the_run_starts(api, login, add_uploaded, worker, services, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "12-5678_1.jpg", "1-2345.jpg"])
    fail_on("media", status=400, match="12-5678")
    fail_on("media", status=500, match="1-2345")
    run_once(api, job, worker)
    api.post(f"/api/jobs/{job}/fix")
    assert api.delete(f"/api/jobs/{job}/rows/3").status_code == 200
    assert [d["file"] for d in services.storage.get_job(job)["deletedRows"]] == ["1-2345.jpg"]
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    j = api.get(f"/api/jobs/{job}").json()
    assert [d["file"] for d in j["runs"][1]["deletedBefore"]] == ["1-2345.jpg"]
    assert j["job"]["deletedRows"] == []


def test_a_fixs_expiry_is_kept_in_draft_expires_at_not_the_ttl_attribute(api, login, add_uploaded, worker, services, fail_on):
    login()
    job = new_job(api)
    add_uploaded(job, ["12-5678_1.jpg", "15-1234_1.jpg"])
    fail_on("media", status=400, match="12-5678")
    run_once(api, job, worker)
    shown = api.post(f"/api/jobs/{job}/fix").json()
    item = services.storage.get_job(job)
    assert item.get("expiresAt") is None and item["draftExpiresAt"] > now() + 29 * 86400
    assert shown["expiresAt"] == item["draftExpiresAt"]  # the API still shows it as the draft's expiresAt
    listed = {x["id"]: x for x in api.get("/api/jobs").json()["jobs"]}[job]
    assert listed["expiresAt"] == item["draftExpiresAt"]
    services.storage.update_job(job, {"draftExpiresAt": now() + 100})
    api.patch(f"/api/jobs/{job}/rows/1", json={"description": "saved again"})  # a save restarts it, still there
    item = services.storage.get_job(job)
    assert item.get("expiresAt") is None and item["draftExpiresAt"] > now() + 29 * 86400
    assert worker.sweep_expired_drafts() == []
    services.storage.update_job(job, {"draftExpiresAt": now() - 1})
    assert worker.sweep_expired_drafts() == [job]
    item = services.storage.get_job(job)
    assert item["status"] == "NeedsAttention" and item.get("draftExpiresAt") is None and item.get("expiresAt") is None
    # a fix that is scheduled and runs leaves no draft expiry behind
    api.post(f"/api/jobs/{job}/fix")
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    worker.tick()
    item = services.storage.get_job(job)
    assert item["status"] == "Completed" and item.get("draftExpiresAt") is None and item["expiresAt"] > now() + 29 * 86400


# ---- an unexpected error ends the run the normal way (code "unknown") ------------------------------------------
def test_an_unexpected_error_during_a_row_finishes_the_job_as_unknown(api, login, add_uploaded, worker, services, caplog):
    """Anything the worker doesn't expect (here DynamoDB refusing a write) ends the run at once: the job fails with
    code unknown and a technical detail naming the exception type and where, never its message; the saved sign-in
    is deleted, the run lock released, and the run item and audit entry written as for any other ending."""
    from botocore.exceptions import ClientError
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg", "12-5678_1.jpg"])
    real = worker._delete_staged

    def broken(row):
        if row["n"] == 2:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "secret-ish text"}}, "UpdateItem")
        return real(row)
    worker._delete_staged = broken
    j = run_once(api, job, worker)
    assert j["job"]["status"] == "Failed" and j["job"]["code"] == "unknown"
    assert j["job"]["codeDetail"] == ("Unexpected ClientError ProvisionedThroughputExceededException in UpdateItem "
                                      "at document 2, step upload")
    assert "secret-ish" not in j["job"]["codeDetail"]
    assert j["runs"][0]["outcome"] == "Failed" and j["runs"][0]["code"] == "unknown"
    assert j["runs"][0]["codeDetail"] == j["job"]["codeDetail"]
    assert [r["result"]["state"] for r in j["rows"]] == ["Done", "Partial"]  # the row it was on is settled
    assert j["rows"][1]["result"]["interrupted"] == 1
    assert services.storage.get_credential(job) is None
    assert services.storage.acquire_lock("pahma", "someone-else", 60)  # released
    run_entry = next(a for a in services.storage.list_audit("pahma") if a["type"] == "Run")
    assert "Run 1: Failed (unknown)" in run_entry["detail"] and run_entry["codeDetail"] == j["job"]["codeDetail"]
    assert "unexpected error at document 2, step upload" in caplog.text  # logged with its traceback
    # Reschedule continues: only what isn't done runs again
    api.post(f"/api/jobs/{job}/fix")
    worker._delete_staged = real
    services.storage.release_lock("pahma", "someone-else")
    j = run_once(api, job, worker)
    assert j["job"]["status"] == "Completed" and j["job"]["codeDetail"] == ""


def test_an_unexpected_error_before_any_row_names_the_start_and_never_the_password(api, login, add_uploaded, worker, services):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200

    def cannot_decrypt(*a, **k):
        raise ValueError("the password was admin")
    worker.crypto = type("Broken", (), {"decrypt": staticmethod(cannot_decrypt)})()
    assert worker.tick() is True
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "Failed" and j["job"]["code"] == "unknown"
    assert j["job"]["codeDetail"] == "Unexpected ValueError while starting the run"
    assert j["rows"][0]["result"] is None and j["job"]["counts"]["notStarted"] == 1
    assert services.storage.get_credential(job) is None


def test_no_job_starts_while_another_of_the_tenants_jobs_is_running(api, login, add_uploaded, worker, services):
    """The run lock can be lost (it expired, or the run's worker stopped): a job still Running keeps the next one
    waiting until it ends; the heartbeat check ends it if its worker is gone."""
    login()
    running, queued = new_job(api, "running"), new_job(api, "queued")
    add_uploaded(running, ["15-1234_1.jpg"])
    add_uploaded(queued, ["12-5678_1.jpg"])
    for j in (running, queued):
        assert api.post(f"/api/jobs/{j}/schedule").status_code == 200
    services.storage.update_job(running, {"status": "Running", "run": 1, "startedAt": now(), "heartbeatAt": now()})
    assert worker.tick() is False
    assert services.storage.get_job(queued)["status"] == "Queued"
    # its worker stopped: the heartbeat check ends it, and then the queued job runs
    services.storage.update_job(running, {"heartbeatAt": now() - 600, "startedAt": now() - 900})
    worker._last_sweep = 0
    assert worker.tick() is True
    assert services.storage.get_job(running)["code"] == "worker_stopped"
    assert services.storage.get_job(queued)["status"] == "Completed"


def test_audit_entries_expire_through_the_tables_ttl(services):
    """Design (Retention and audit): audit entries are kept 365 days, by DynamoDB TTL on "expires"."""
    client = services.storage.dynamodb.meta.client
    for table in ("t-audit", "t-sessions", "t-credentials"):
        ttl = client.describe_time_to_live(TableName=table)["TimeToLiveDescription"]
        assert ttl["TimeToLiveStatus"] == "ENABLED" and ttl["AttributeName"] == "expires"
    ttl = client.describe_time_to_live(TableName="t-jobs")["TimeToLiveDescription"]
    assert ttl["TimeToLiveStatus"] == "DISABLED"  # the sweeper expires jobs, auditing them first


# ---- the run item points to its audit entry, written in one transaction (design: Job data model, Run items) ----------
def _entry(services, key):
    return services.storage.audit_table.get_item(Key={"PK": "TENANT#pahma", "SK": key}).get("Item")


def test_a_finished_run_points_to_its_audit_entry(api, login, add_uploaded, worker, services):
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    j = run_once(api, job, worker)
    run = j["runs"][0]
    assert run["outcome"] == "Completed" and run["auditKey"]
    entry = _entry(services, run["auditKey"])
    assert entry["type"] == "Run" and entry["job"] == job and int(entry["run"]) == 1
    assert entry["detail"].startswith("Run 1: Completed")


def test_a_failed_transaction_leaves_no_finished_run_without_its_entry(api, login, add_uploaded, worker, services, monkeypatch):
    """The run item and the Run entry are written together or not at all: here DynamoDB cancels the transaction (a
    condition that fails is added to it), so neither is written and the job stays Running; the heartbeat check
    then finishes the run as worker_stopped, with its entry."""
    import pytest
    from botocore.exceptions import ClientError
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    client = services.storage.dynamodb.meta.client
    real = client.transact_write_items
    failed = []

    def cancelled(TransactItems, **kw):
        if not failed and any(i.get("Put", {}).get("Item", {}).get("type") == "Run" for i in TransactItems):
            failed.append(1)
            TransactItems = [*TransactItems, {"ConditionCheck": {"TableName": services.storage.jobs.name,
                                                                 "Key": {"PK": "NO#SUCH", "SK": "ITEM"},
                                                                 "ConditionExpression": "attribute_exists(PK)"}}]
        return real(TransactItems=TransactItems, **kw)
    monkeypatch.setattr(client, "transact_write_items", cancelled)
    assert api.post(f"/api/jobs/{job}/schedule").status_code == 200
    with pytest.raises(ClientError):
        worker.tick()
    assert failed
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "Running"
    assert j["runs"][0]["outcome"] == "Running" and "auditKey" not in j["runs"][0]
    assert not [a for a in services.storage.list_audit("pahma") if a["type"] == "Run"]
    assert services.storage.get_credential(job) is None  # the password is gone anyway
    # the heartbeat check finishes it, with its entry
    services.storage.update_job(job, {"heartbeatAt": now() - 600})
    assert worker.sweep_stopped_jobs() == [job]
    j = api.get(f"/api/jobs/{job}").json()
    run = j["runs"][0]
    assert j["job"]["status"] == "Failed" and j["job"]["code"] == "worker_stopped" and run["outcome"] == "Failed"
    assert _entry(services, run["auditKey"])["detail"].startswith("Run 1: Failed (worker_stopped)")
    assert len([a for a in services.storage.list_audit("pahma") if a["type"] == "Run"]) == 1


def test_a_run_already_finished_with_its_entry_is_not_finished_again(api, login, add_uploaded, worker, services):
    """If the worker stopped after the transaction but before updating the job, the heartbeat check sets the job
    from the recorded run and writes no second entry."""
    login()
    job = new_job(api)
    add_uploaded(job, ["15-1234_1.jpg"])
    run_once(api, job, worker)
    services.storage.update_job(job, {"status": "Running", "code": "", "heartbeatAt": now() - 600})
    assert worker.sweep_stopped_jobs() == [job]
    j = api.get(f"/api/jobs/{job}").json()
    assert j["job"]["status"] == "Completed" and j["job"]["code"] == "" and j["job"]["codeDetail"] == ""
    assert len([a for a in services.storage.list_audit("pahma") if a["type"] == "Run"]) == 1
