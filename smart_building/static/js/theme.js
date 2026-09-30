(() => {
  const apply = (theme) => {
    const value = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-bs-theme", value);
    localStorage.setItem("sb-theme", value);
    document.querySelectorAll("[data-theme-toggle] i").forEach((icon) => {
      icon.className = value === "dark" ? "bi bi-sun" : "bi bi-moon-stars";
    });
  };
  document.addEventListener("DOMContentLoaded", () => {
    apply(localStorage.getItem("sb-theme") || "light");
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => button.addEventListener("click", () => {
      apply(document.documentElement.getAttribute("data-bs-theme") === "dark" ? "light" : "dark");
    }));
    document.querySelectorAll("[data-set-theme]").forEach((button) => button.addEventListener("click", () => apply(button.dataset.setTheme)));
  });
})();

