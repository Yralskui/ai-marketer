const token = location.hash.slice(1);
history.replaceState(null, "", location.pathname);
const form = document.querySelector("form");
const feedback = document.querySelector("#status");
if (!/^[A-Za-z0-9_-]{43}$/.test(token)) {
    form.hidden = true;
    feedback.textContent = "Откройте новую ссылку из бота.";
}
form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = document.querySelector("#file");
    const file = input.files[0];
    if (!file || !file.size || file.size > 100_000_000) {
        feedback.textContent = "Выберите непустой файл до 100 МБ.";
        return;
    }
    form.querySelector("button").disabled = true;
    input.disabled = true;
    feedback.textContent = "Загружаю документ…";
    try {
        const response = await fetch("/upload", {
            method: "POST",
            headers: {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/octet-stream",
                "X-Filename": encodeURIComponent(file.name),
            },
            body: file,
            signal: AbortSignal.timeout(250_000),
        });
        feedback.textContent = response.ok
            ? "Файл сохранён. Можно вернуться в бот."
            : await response.text();
    } catch {
        feedback.textContent = "Связь прервалась. Проверьте результат в боте или запросите новую ссылку.";
    }
});
