const form = document.getElementById("loginForm");
const error = document.getElementById("loginError");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  error.textContent = "";
  try {
    const response = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: document.getElementById("loginUsername").value.trim(), password: document.getElementById("loginPassword").value, remember: document.getElementById("rememberLogin").checked }) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "登录失败");
    const next = new URLSearchParams(location.search).get("next") || "/";
    location.href = next.startsWith("/") && !next.startsWith("//") ? next : "/";
  } catch (loginError) {
    error.textContent = loginError.message;
    button.disabled = false;
  }
});
