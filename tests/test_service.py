from __future__ import annotations

import pytest

from putio_mcp.errors import CONFLICT, INVALID, NOT_FOUND, PERMISSION, PutioError


# --------------------------------------------------------------------------- #
# pagination                                                                   #
# --------------------------------------------------------------------------- #


def test_list_files_paginates_with_cursor(fake, service):
    folder = fake.add_folder("Movies")
    for i in range(7):
        fake.add_file(f"movie{i}.mp4", folder)

    page1 = service.list_files(folder, per_page=3)
    assert page1["folder"]["id"] == folder and page1["folder"]["name"] == "Movies"
    assert [f["name"] for f in page1["items"]] == ["movie0.mp4", "movie1.mp4", "movie2.mp4"]
    assert page1["total"] == 7 and page1["count"] == 3
    assert page1["next_cursor"]

    page2 = service.list_files(folder, per_page=3, cursor=page1["next_cursor"])
    assert [f["name"] for f in page2["items"]] == ["movie3.mp4", "movie4.mp4", "movie5.mp4"]
    assert page2["folder"] is None  # continuation pages carry no parent

    page3 = service.list_files(folder, per_page=3, cursor=page2["next_cursor"])
    assert [f["name"] for f in page3["items"]] == ["movie6.mp4"]
    assert page3["next_cursor"] is None


def test_list_files_root_has_synthetic_folder(fake, service):
    fake.add_file("a.mp4")
    result = service.list_files()
    assert result["folder"]["id"] == 0 and result["folder"]["is_folder"] is True
    assert result["items"][0]["parent_id"] == 0


@pytest.mark.parametrize("per_page", [0, -1, 1001, "10", True])
def test_list_files_rejects_bad_per_page(service, per_page):
    with pytest.raises(PutioError) as e:
        service.list_files(0, per_page=per_page)
    assert e.value.kind == INVALID


def test_list_files_rejects_bad_sort_and_negative_folder(service):
    with pytest.raises(PutioError) as e:
        service.list_files(0, sort_by="RANDOM")
    assert e.value.kind == INVALID and "sort_by" in e.value.message
    with pytest.raises(PutioError):
        service.list_files(-1)


def test_list_files_accepts_lowercase_sort(fake, service):
    service.list_files(0, sort_by="name_desc")
    assert fake.calls[-1] == ("list_folder", (0, 100, "NAME_DESC", None))


def test_search_paginates_and_rejects_empty_query(fake, service):
    for i in range(5):
        fake.add_file(f"show.s01e0{i}.mkv")
    fake.add_file("unrelated.txt")
    page = service.search_files("show", per_page=2)
    assert page["count"] == 2 and page["total"] == 5 and page["next_cursor"]
    page2 = service.search_files("show", per_page=2, cursor=page["next_cursor"])
    assert page2["count"] == 2
    with pytest.raises(PutioError) as e:
        service.search_files("   ")
    assert e.value.kind == INVALID


def test_get_file_shapes_output(fake, service):
    folder = fake.add_folder("TV")
    fid = fake.add_file("ep.mkv", folder, size=99)
    info = service.get_file(fid)
    assert info == {
        "id": fid,
        "name": "ep.mkv",
        "parent_id": folder,
        "is_folder": False,
        "file_type": "VIDEO",
        "content_type": "video/mp4",
        "size": 99,
        "created_at": "2026-01-01T00:00:00",
        "is_shared": False,
    }
    assert service.get_file(0)["name"] == "Your Files"
    with pytest.raises(PutioError) as e:
        service.get_file(999_999)
    assert e.value.kind == NOT_FOUND


# --------------------------------------------------------------------------- #
# destination validation                                                       #
# --------------------------------------------------------------------------- #


def test_create_folder_rejects_missing_or_file_parent(fake, service):
    with pytest.raises(PutioError) as e:
        service.create_folder("New", parent_id=424242)
    assert e.value.kind == NOT_FOUND and "424242" in e.value.message

    a_file = fake.add_file("notes.txt")
    with pytest.raises(PutioError) as e:
        service.create_folder("New", parent_id=a_file)
    assert e.value.kind == INVALID and "not a folder" in e.value.message
    assert not any(c[0] == "create_folder" for c in fake.calls)


@pytest.mark.parametrize("bad", ["", "   ", ".", "..", "a/b", "a\\b", "x\x00y", "tab\there", "n" * 256])
def test_create_folder_rejects_invalid_names(service, bad):
    with pytest.raises(PutioError) as e:
        service.create_folder(bad, parent_id=0)
    assert e.value.kind == INVALID


def test_create_folder_strips_and_creates(fake, service):
    result = service.create_folder("  Season 1  ", parent_id=0)
    assert result["created"]["name"] == "Season 1"
    assert result["created"]["parent_id"] == 0
    assert result["parent"]["id"] == 0


def test_create_folder_dry_run_changes_nothing(fake, service):
    result = service.create_folder("Preview", parent_id=0, dry_run=True)
    assert result["dry_run"] is True and result["would_create"]["name"] == "Preview"
    assert not any(c[0] == "create_folder" for c in fake.calls)


def test_move_rejects_invalid_destination(fake, service):
    fid = fake.add_file("a.mp4")
    with pytest.raises(PutioError) as e:
        service.move_files([fid], destination_id=fid)
    assert e.value.kind == INVALID and "not a folder" in e.value.message
    with pytest.raises(PutioError) as e:
        service.move_files([fid], destination_id=777777)
    assert e.value.kind == NOT_FOUND
    with pytest.raises(PutioError):
        service.move_files([fid], destination_id=-5)
    with pytest.raises(PutioError):
        service.move_files([], destination_id=0)


def test_add_transfer_validates_destination(fake, service):
    folder = fake.add_folder("Downloads")
    result = service.add_transfer("magnet:?xt=urn:btih:abc", destination_id=folder)
    assert result["destination"]["id"] == folder
    assert fake.calls[-1] == ("add_transfer", ("magnet:?xt=urn:btih:abc", folder))

    a_file = fake.add_file("x.mp4")
    with pytest.raises(PutioError) as e:
        service.add_transfer("https://example.com/f.iso", destination_id=a_file)
    assert e.value.kind == INVALID
    with pytest.raises(PutioError) as e:
        service.add_transfer("ftp://example.com/f.iso")
    assert e.value.kind == INVALID

    result = service.add_transfer("https://example.com/f.iso")
    assert result["destination"] is None
    assert fake.calls[-1] == ("add_transfer", ("https://example.com/f.iso", None))


# --------------------------------------------------------------------------- #
# conflicting names                                                            #
# --------------------------------------------------------------------------- #


def test_create_folder_reports_conflict_case_insensitively(fake, service):
    existing = fake.add_folder("Movies")
    with pytest.raises(PutioError) as e:
        service.create_folder("movies", parent_id=0)
    assert e.value.kind == CONFLICT
    assert str(existing) in e.value.message and "Movies" in e.value.message
    assert e.value.details["existing"]["id"] == existing


def test_rename_reports_conflict_and_noop(fake, service):
    folder = fake.add_folder("TV")
    a = fake.add_file("a.mkv", folder)
    fake.add_file("b.mkv", folder)
    with pytest.raises(PutioError) as e:
        service.rename_file(a, "B.MKV")
    assert e.value.kind == CONFLICT and "b.mkv" in e.value.message

    same = service.rename_file(a, "a.mkv")
    assert same["changed"] is False
    assert not any(c[0] == "rename" for c in fake.calls)


def test_rename_success_and_dry_run(fake, service):
    a = fake.add_file("old.mkv")
    preview = service.rename_file(a, "new.mkv", dry_run=True)
    assert preview["dry_run"] is True and preview["would_rename_to"] == "new.mkv"
    assert fake.files[a]["name"] == "old.mkv"

    result = service.rename_file(a, "new.mkv")
    assert result["changed"] is True
    assert result["previous_name"] == "old.mkv" and result["item"]["name"] == "new.mkv"
    assert fake.files[a]["name"] == "new.mkv"


def test_rename_rejects_root_missing_and_bad_names(fake, service):
    with pytest.raises(PutioError) as e:
        service.rename_file(0, "root")
    assert e.value.kind == INVALID
    with pytest.raises(PutioError) as e:
        service.rename_file(123456, "x")
    assert e.value.kind == NOT_FOUND
    a = fake.add_file("a.mkv")
    with pytest.raises(PutioError) as e:
        service.rename_file(a, "bad/name")
    assert e.value.kind == INVALID


def test_move_reports_conflict_with_destination_and_within_batch(fake, service):
    dest = fake.add_folder("Dest")
    fake.add_file("dupe.mkv", dest)
    src = fake.add_folder("Src")
    x = fake.add_file("dupe.mkv", src)
    y = fake.add_file("fresh.mkv", src)
    other = fake.add_folder("Other")
    z = fake.add_file("FRESH.mkv", other)

    result = service.move_files([x, y, z], dest)
    by_id = {r["file_id"]: r for r in result["results"]}
    assert by_id[x]["status"] == "failed" and by_id[x]["error_kind"] == CONFLICT
    assert by_id[y]["status"] == "moved"
    assert by_id[z]["status"] == "failed" and "another item in this request" in by_id[z]["reason"]
    assert result["summary"] == {"moved": 1, "would_move": 0, "skipped": 0, "failed": 2}
    assert result["all_succeeded"] is False
    assert fake.files[y]["parent_id"] == dest and fake.files[x]["parent_id"] == src


# --------------------------------------------------------------------------- #
# invalid moves                                                                #
# --------------------------------------------------------------------------- #


def test_move_refuses_folder_into_itself_or_descendant(fake, service):
    top = fake.add_folder("Top")
    mid = fake.add_folder("Mid", top)
    leaf = fake.add_folder("Leaf", mid)
    unrelated = fake.add_file("ok.mkv")

    result = service.move_files([top, mid, leaf, unrelated], destination_id=leaf)
    by_id = {r["file_id"]: r for r in result["results"]}
    assert by_id[top]["status"] == "failed" and "its own descendant" in by_id[top]["reason"]
    assert by_id[mid]["status"] == "failed" and "its own descendant" in by_id[mid]["reason"]
    assert by_id[leaf]["status"] == "failed" and "itself" in by_id[leaf]["reason"]
    assert by_id[unrelated]["status"] == "moved"
    assert fake.files[top]["parent_id"] == 0 and fake.files[mid]["parent_id"] == top


def test_move_skips_items_already_in_destination_and_duplicates(fake, service):
    dest = fake.add_folder("Dest")
    here = fake.add_file("here.mkv", dest)
    there = fake.add_file("there.mkv")
    result = service.move_files([here, there, there], dest)
    statuses = [(r["file_id"], r["status"]) for r in result["results"]]
    assert statuses == [(here, "skipped"), (there, "moved"), (there, "skipped")]
    assert result["summary"]["skipped"] == 2


def test_move_reports_missing_and_malformed_ids_without_aborting(fake, service):
    dest = fake.add_folder("Dest")
    good = fake.add_file("good.mkv")
    result = service.move_files([999_999, -1, 0, good], dest)
    by_pos = result["results"]
    assert by_pos[0]["status"] == "failed" and by_pos[0]["error_kind"] == NOT_FOUND
    assert by_pos[1]["status"] == "failed" and by_pos[1]["error_kind"] == INVALID
    assert by_pos[2]["status"] == "failed" and "root" in by_pos[2]["reason"]
    assert by_pos[3]["status"] == "moved"
    assert fake.files[good]["parent_id"] == dest


def test_move_dry_run_performs_no_moves(fake, service):
    dest = fake.add_folder("Dest")
    a = fake.add_file("a.mkv")
    result = service.move_files([a], dest, dry_run=True)
    assert result["dry_run"] is True
    assert result["results"][0]["status"] == "would_move"
    assert result["results"][0]["from_parent_id"] == 0
    assert result["summary"]["would_move"] == 1
    assert not any(c[0] == "move" for c in fake.calls)
    assert fake.files[a]["parent_id"] == 0


# --------------------------------------------------------------------------- #
# partial failures                                                             #
# --------------------------------------------------------------------------- #


def test_move_reports_per_item_api_failures(fake, service):
    dest = fake.add_folder("Dest")
    a = fake.add_file("a.mkv")
    b = fake.add_file("b.mkv")
    c = fake.add_file("c.mkv")
    fake.fail_move_ids.add(b)

    result = service.move_files([a, b, c], dest)
    by_id = {r["file_id"]: r for r in result["results"]}
    assert by_id[a]["status"] == "moved"
    assert by_id[b]["status"] == "failed" and by_id[b]["error_kind"] == PERMISSION
    assert by_id[c]["status"] == "moved"
    assert result["summary"] == {"moved": 2, "would_move": 0, "skipped": 0, "failed": 1}
    assert result["all_succeeded"] is False
    assert result["destination"]["name"] == "Dest"


def test_move_uses_destination_listing_for_conflicts_across_pages(fake, service):
    dest = fake.add_folder("Big")
    for i in range(1500):  # more than one put.io page
        fake.add_file(f"f{i}.mkv", dest)
    clash = fake.add_file("f1499.mkv")
    result = service.move_files([clash], dest)
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["error_kind"] == CONFLICT


# --------------------------------------------------------------------------- #
# transfers                                                                    #
# --------------------------------------------------------------------------- #


def test_transfer_tools(fake, service):
    tid = fake.add_transfer_record("Ubuntu ISO", status="DOWNLOADING")
    listed = service.list_transfers()
    assert listed[0]["id"] == tid and listed[0]["status"] == "DOWNLOADING"

    link = service.get_browser_link(tid)
    assert link["url"] is None and "not completed" in link["message"]

    done = fake.add_transfer_record("Done", status="COMPLETED", file_id=55)
    assert service.get_browser_link(done)["url"] == "https://app.put.io/files/55"

    assert service.cancel_transfer(tid)["cancelled"]["name"] == "Ubuntu ISO"
    assert tid not in fake.transfers
    with pytest.raises(PutioError) as e:
        service.cancel_transfer(tid)
    assert e.value.kind == NOT_FOUND
