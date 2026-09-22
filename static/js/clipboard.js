/* Copy-title-to-clipboard with fallback for non-secure origins. */
export async function copyTitle(btn) {
  const text = btn.dataset.copy;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    btn.classList.add("ok");
    btn.textContent = "✓";
    setTimeout(() => { btn.classList.remove("ok"); btn.textContent = "⧉"; }, 1200);
  } catch (err) { alert(`Could not copy "${text}": ${err.message}`); }
}
