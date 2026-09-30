import { api, explainError } from "./core.js";

document.getElementById("login").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  err.classList.add("hidden");
  try {
    await api("POST", "/api/login", { password: document.getElementById("pw").value });
    location.href = "/o";
  } catch (ex) {
    err.textContent = explainError(ex);
    err.classList.remove("hidden");
  }
});
