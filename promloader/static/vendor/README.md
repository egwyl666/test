# Сторонние файлы

| Файл | Что это | Версия | Лицензия | Откуда |
|---|---|---|---|---|
| `zxing-0.21.3.min.js` | ZXing — распознавание штрихкодов и QR-кодов на JS (сборка UMD, `window.ZXing`) | 0.21.3 | Apache-2.0 (`zxing-LICENSE.txt`) | npm `@zxing/library@0.21.3`, файл `umd/index.min.js`, без изменений; sha256 `d7cc8f69dd70bdcf3ac00c9ae572bf2acb9f4132ba379c72df842e4db918652d` |
| `lucide-1.49.0.svg` | Значки Lucide — выборка из 53 штук одним SVG-спрайтом (`<symbol id="имя">`), заменяют эмодзи в кнопках и заголовках (`common.js` → `ICONS`) | 1.49.0 | ISC (`lucide-LICENSE.txt`) | npm `lucide-static@1.49.0`, файлы `icons/<имя>.svg`, только содержимое `<svg>` перенесено в `<symbol>`; файл собран из них, sha256 спрайта `21b7b9824bb888127a8725ca886aab51193f20b5cd6519f430f67df3d96dbaa2` |

Сканер (`../scanner.js`) загружает ZXing только там, где браузер не умеет распознавать штрихкоды сам (`BarcodeDetector`
нет — iPhone, Firefox). Файл отдаётся программой, интернет для сканирования не нужен.
