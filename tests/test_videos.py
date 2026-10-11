"""Видео YouTube: из прайсов поставщиков и своих файлов, в карточке, с 🔒 и в журнале."""

import json

import pytest

from promloader import db, excel, products, suppliers

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YT2 = "https://www.youtube.com/watch?v=9bZkp7q19f0"


@pytest.mark.parametrize("url", ["https://youtu.be/dQw4w9WgXcQ", "youtube.com/watch?v=dQw4w9WgXcQ&t=42",
                                 "https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
                                 "https://www.youtube.com/shorts/dQw4w9WgXcQ", "https://www.youtube.com/embed/dQw4w9WgXcQ?rel=0",
                                 "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", "https://www.youtube.com/live/dQw4w9WgXcQ"])
def test_youtube_links_in_one_form(url):
    assert products.youtube_url(url) == YT


def test_split_and_parse_videos():
    assert products.split_videos(f"{YT}, https://youtu.be/dQw4w9WgXcQ; https://vimeo.com/5 {YT2}") == (
        [YT, YT2], ["https://vimeo.com/5"])
    assert products.split_videos(json.dumps([YT])) == ([YT], [])
    assert products.youtube_url("https://www.youtube.com/watch?v=short") is None
    with pytest.raises(products.ProductError, match="только ссылки YouTube"):
        products.parse_videos("https://vimeo.com/5")
    with pytest.raises(products.ProductError, match="Не больше 5"):
        products.parse_videos([f"https://youtu.be/{i:011d}" for i in range(6)])


def test_product_videos_saved_locked_and_logged(client):
    pid = products.create({"name": "Кружка", "price": 10, "videos": ["https://youtu.be/dQw4w9WgXcQ"]})
    assert products.get(pid)["videos"] == [YT]
    r = client.patch(f"/api/products/{pid}", json={"videos": [YT, "https://youtu.be/9bZkp7q19f0"]})
    assert r.status_code == 200 and r.json()["videos"] == [YT, YT2]
    r = client.patch(f"/api/products/{pid}", json={"videos": ["https://vimeo.com/5"]})
    assert r.status_code == 400 and r.json().get("field") == "videos"
    log = client.get(f"/api/products/{pid}/changes").json()
    entry = next(c for c in log["items"] if c["field"] == "videos")
    assert entry["new"] == f"{YT}\n{YT2}"


def test_guess_mapping_and_xml_video_tags(tmp_path):
    m = excel.guess_mapping(["Артикул", "Видео товара", "Название", "Цена", "param:Відео", "param:Видеокарта", "YouTube"])
    assert m == {"A": "external_id", "B": "videos", "C": "name", "D": "price", "E": "videos", "F": "param", "G": "videos"}
    xml = tmp_path / "f.xml"
    xml.write_text(f"""<?xml version="1.0" encoding="utf-8"?><yml_catalog><shop><offers>
      <offer id="A1"><name>Кружка</name><price>10</price><video url="{YT}"/></offer>
    </offers></shop></yml_catalog>""", encoding="utf-8")
    rows, _ = excel.read_sheet(xml, "XML")
    assert rows[0] == ["@id", "name", "price", "video"] and rows[1][3] == YT


def test_build_products_takes_youtube_and_warns_about_others():
    rows = [["Артикул", "Название", "Цена", "Видео", "Видео 2"],
            ["A1", "Кружка", "10", "https://youtu.be/dQw4w9WgXcQ https://vimeo.com/5", YT2]]
    items = excel.build_products(rows, 1, [2], {"A": "external_id", "B": "name", "C": "price", "D": "videos", "E": "videos"})
    item = items[0]
    assert json.loads(item["data"]["videos"]) == [YT, YT2]
    assert any("не на YouTube" in w for w in item["warnings"])


def test_supplier_videos_follow_price_list_but_not_over_lock():
    content = f"""<?xml version="1.0" encoding="utf-8"?><yml_catalog><shop><offers>
      <offer id="A1"><name>Кружка</name><price>100</price><video>{YT}</video></offer>
    </offers></shop></yml_catalog>""".encode()
    sid = suppliers.create("Видео-опт")
    suppliers.store_source(sid, content, "feed.xml")
    suppliers.update(sid, {"mapping": {"A": "external_id", "B": "name", "C": "cost_price", "D": "videos"}, "header_row": 1})
    assert suppliers.run(sid)["status"] == "ok"
    pid = db.query_one("SELECT id FROM products WHERE external_id = 'A1'")["id"]
    assert products.get(pid)["videos"] == [YT]

    suppliers.store_source(sid, content.replace(b"dQw4w9WgXcQ", b"9bZkp7q19f0"), "feed.xml")
    assert suppliers.run(sid)["status"] == "ok"
    assert products.get(pid)["videos"] == [YT2]  # поставщик сменил видео — у нас тоже

    products.update(pid, {"videos": []})  # своё: видео убрали руками
    assert "videos" in products.get(pid)["locked_fields"]
    suppliers.store_source(sid, content, "feed.xml")
    suppliers.run(sid)
    assert products.get(pid)["videos"] == []  # 🔒 — прайс не возвращает


def test_feed_puts_videos_as_players_into_description():
    import xml.etree.ElementTree as ET

    from promloader import feed
    pid = products.create({"name": "Кружка", "price": 10, "description": "Белая кружка.",
                           "description_ua": "Білий кухоль.", "videos": [YT, YT2]})
    bare = products.create({"name": "Чашка", "price": 5, "videos": [YT]})
    already = products.create({"name": "Блюдце", "price": 5, "videos": [YT],
                               "description": '<p>Смотрите:</p><iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>'})
    root = ET.fromstring(feed.build([pid, bare, already], "https://shop.example.com"))
    offers = {o.findtext("name"): o for o in root.iter("offer")}
    desc = offers["Кружка"].findtext("description")
    assert desc.startswith("<p>Белая кружка.</p>") and desc.count("<iframe") == 2
    assert 'src="https://www.youtube.com/embed/dQw4w9WgXcQ"' in desc and "embed/9bZkp7q19f0" in desc
    assert offers["Кружка"].findtext("description_ua").count("<iframe") == 2
    assert offers["Чашка"].findtext("description").count("<iframe") == 1  # описания нет — только плеер
    assert offers["Блюдце"].findtext("description").count("dQw4w9WgXcQ") == 1  # уже есть в описании — не дублируем
    assert products.get(pid)["description"] == "Белая кружка."  # в программе описание не меняется
