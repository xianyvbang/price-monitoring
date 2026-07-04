document.getElementById("options").addEventListener("click", () => {
  chrome.runtime.openOptionsPage(() => {
    const err = chrome.runtime.lastError;
    document.getElementById("status").textContent = err ? err.message : "";
  });
});
