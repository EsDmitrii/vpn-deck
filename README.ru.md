<div align="center">
  <img src="assets/logo.png" alt="VPN Deck" width="88" height="88">
  <h1>VPN Deck</h1>
  <p><strong>Вставил ссылку. Выбрал сервер. Включил.</strong></p>
  <p>VPN прямо из игрового режима Steam Deck: AmneziaWG, VLESS (REALITY) и v2ray-подписки.</p>
  <p>
    <a href="#установка"><strong>Установить</strong></a>
    · <a href="#быстрый-старт">Быстрый старт</a>
    · <a href="README.md">English</a>
  </p>
  <img src="assets/screenshot.jpeg" alt="VPN Deck в меню быстрого доступа Steam Deck" width="420">
</div>

## Установка

Для обоих способов нужен режим разработчика: Decky Loader → настройки → **General** → **Developer mode**.

**По ссылке.** Decky Loader → настройки → **Developer** → **Install Plugin from URL**, наберите:

```
https://github.com/mrwaip/vpn-deck/releases/latest/download/vpn-deck.zip
```

**Из файла.**

1. Скачайте `vpn-deck.zip` из [Releases](https://github.com/mrwaip/vpn-deck/releases/latest).
2. Перенесите его на Deck: например, через LocalSend в «Загрузки» или скачайте в режиме рабочего стола.
3. Decky Loader → настройки → **Developer** → **Install Plugin from ZIP File** → выберите файл.

Перед обновлением или удалением выключите VPN в плагине.

## Быстрый старт

1. Откройте плагин → **Импортировать конфиг**.
2. Вставьте ссылку или выберите файл:
   - **AmneziaVPN** — при экспорте **Protocol: AmneziaWG**, затем:
     - **Connection Format: For the AmneziaVPN app** → ссылка `vpn://`, вставьте её;
     - **Connection Format: AmneziaWG native format** → файл `.conf`, выберите его.
   - **VLESS** — ссылка `vless://` или URL подписки от провайдера.
   - **sing-box** — файл `.json`.
3. Для подписки выберите сервер. Потом его можно сменить в списке.
4. Включите переключатель у конфига.

Туннель поднялся, но ничего не открывается? Переимпортируйте AWG-конфиг с **MTU 1280**.

## Дисклеймер

**Ограничение ответственности:** плагин — технический инструмент для управления сетевыми конфигурациями (AmneziaWG, VLESS). Использование — на ваш страх и риск. Вы самостоятельно отвечаете за соблюдение законодательства вашей страны или региона. Разработчик не призывает к нарушению законов и не несёт ответственности за способ и цели использования плагина.

Плагин разрабатывался чисто на энтузиазме, в свободное время. При проблемах заводите детальный issue на [GitHub](https://github.com/mrwaip/vpn-deck/issues): опишите шаги воспроизведения, версию плагина и системы, приложите логи при необходимости.

<sub>BSD-3-Clause · AmneziaWG и sing-box включены в релиз под своими лицензиями.</sub>
