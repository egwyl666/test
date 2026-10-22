"""2.29.3: по итогам обхода всех страниц — читаемый цвет из логотипа, понятная страница «Нет доступа», права
«просмотра» на действия, которые ничего не меняют."""

from promloader import branding, db

from .conftest import sign_in


def _contrast_to_white(color: str) -> float:
    rgb = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    return 1.05 / (branding._luminance(rgb) + 0.05)


def test_logo_color_is_darkened_until_white_text_reads():
    for color in ("#e6a700", "#ffff00", "#26852b", "#00e5ff", "#ff80ab"):
        out = branding.readable(color)
        assert _contrast_to_white(out) >= branding.MIN_CONTRAST, (color, out)
    assert branding.readable("#6d3fe0") == "#6d3fe0"  # тёмный цвет не трогаем


def test_stored_light_accent_is_fixed_on_read(data_dir):
    db.set_setting("brand_accent", "#e6a700")  # так могла сохранить 2.29.0
    assert _contrast_to_white(branding.accent()) >= branding.MIN_CONTRAST


def test_refused_page_has_way_out_and_theme(anon):
    c = sign_in(anon, login="viewer1", role="viewer")
    r = c.get("/settings")
    assert r.status_code == 403
    assert "Нет доступа" in r.text and 'href="/today"' in r.text
    assert "/static/i18n.js" in r.text and "/static/app.css" in r.text  # тема и перевод, как у всех страниц
    assert "((" not in r.text and "))" not in r.text  # раньше: «(настройки и пользователи (только администратор))»


def test_view_role_may_calculate_and_refresh_but_not_change(anon):
    c = sign_in(anon, login="viewer2", role="viewer")
    assert c.post("/api/pricing/test", json={"cost": "100"}).status_code == 200
    assert c.post("/api/pricing", json={"rules": []}).status_code == 403
    assert c.post("/api/chat/rooms/x/read").status_code == 403
    assert c.post("/api/diagnose", json={"product_id": 1}).status_code == 403
