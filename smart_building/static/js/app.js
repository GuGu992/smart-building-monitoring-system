document.addEventListener("DOMContentLoaded", () => {
  console.info("Data source: Flask + SQLite");
  document.querySelectorAll("[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("tr[data-href]").forEach((row) => {
    row.addEventListener("click", () => { window.location.href = row.dataset.href; });
  });

  if (!window.Chart) return;

  const charts = [];
  const cssValue = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const chartTheme = () => ({
    grid: cssValue("--chart-grid"),
    tick: cssValue("--chart-tick"),
    text: cssValue("--text-primary")
  });

  const source = document.getElementById("chartData");
  if (source) {
    const values = JSON.parse(source.textContent);
    const labels = values.labels.map((value) => new Date(value).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit"
    }));
    const theme = chartTheme();
    const metricCharts = [
      {id: "temperatureChart", data: values.temperature, label: "Temperature", unit: "°C", color: "#e11d48", type: "line"},
      {id: "humidityChart", data: values.humidity, label: "Humidity", unit: "%", color: "#2563eb", type: "line"},
      {id: "airQualityChart", data: values.airQuality, label: "Air Quality", unit: "AQI", color: "#16a34a", type: "line"},
      {id: "energyChart", data: values.energy, label: "Energy Usage", unit: "kWh", color: "#f59e0b", type: "bar"}
    ];

    metricCharts.forEach((metric) => {
      const canvas = document.getElementById(metric.id);
      if (!canvas) return;
      const isBar = metric.type === "bar";
      const chart = new Chart(canvas, {
        type: metric.type,
        data: {
          labels,
          datasets: [{
            label: metric.label,
            data: metric.data,
            borderColor: metric.color,
            backgroundColor: isBar ? "rgba(245, 158, 11, .68)" : "transparent",
            borderWidth: 2,
            borderRadius: isBar ? 4 : 0,
            maxBarThickness: isBar ? 22 : undefined,
            pointBackgroundColor: metric.color,
            pointBorderWidth: 0,
            pointRadius: isBar ? 0 : 2,
            pointHoverRadius: isBar ? 0 : 4,
            tension: isBar ? 0 : .28
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: {mode: "index", intersect: false},
          plugins: {
            legend: {display: false},
            tooltip: {callbacks: {label: (context) => `${context.parsed.y} ${metric.unit}`}}
          },
          scales: {
            x: {
              border: {display: false},
              grid: {display: false},
              ticks: {color: theme.tick, maxTicksLimit: 6, maxRotation: 0}
            },
            y: {
              border: {display: false},
              grid: {color: theme.grid},
              ticks: {color: theme.tick, maxTicksLimit: 5}
            }
          }
        }
      });
      chart.$appChartType = "metric";
      charts.push(chart);
    });
  }

  const statusSource = document.getElementById("buildingStatusData");
  const statusCanvas = document.getElementById("buildingStatusChart");
  if (statusSource && statusCanvas) {
    const values = JSON.parse(statusSource.textContent);
    const centerLabel = {
      id: "centerLabel",
      afterDraw(chart) {
        const {ctx, chartArea} = chart;
        if (!chartArea) return;
        const total = chart.data.datasets[0].data.reduce((sum, value) => sum + Number(value || 0), 0);
        const x = (chartArea.left + chartArea.right) / 2;
        const y = (chartArea.top + chartArea.bottom) / 2;
        ctx.save();
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = chartTheme().text;
        ctx.font = "600 1.45rem system-ui, sans-serif";
        ctx.fillText(total, x, y - 7);
        ctx.fillStyle = chartTheme().tick;
        ctx.font = "500 .68rem system-ui, sans-serif";
        ctx.fillText("EQUIPMENT", x, y + 15);
        ctx.restore();
      }
    };
    const statusChart = new Chart(statusCanvas, {
      type: "doughnut",
      data: {
        labels: ["Normal", "Warning", "Critical"],
        datasets: [{
          data: [values.normal, values.warning, values.critical],
          backgroundColor: [cssValue("--status-normal"), cssValue("--status-warning"), cssValue("--status-critical")],
          borderWidth: 0,
          spacing: 2
        }]
      },
      plugins: [centerLabel],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        cutout: "72%",
        plugins: {legend: {display: false}}
      }
    });
    statusChart.$appChartType = "status";
    charts.push(statusChart);
  }

  if (charts.length) {
    new MutationObserver(() => {
      const theme = chartTheme();
      charts.forEach((chart) => {
        if (chart.$appChartType === "metric") {
          chart.options.scales.x.ticks.color = theme.tick;
          chart.options.scales.y.ticks.color = theme.tick;
          chart.options.scales.y.grid.color = theme.grid;
        } else if (chart.$appChartType === "status") {
          chart.data.datasets[0].backgroundColor = [
            cssValue("--status-normal"), cssValue("--status-warning"), cssValue("--status-critical")
          ];
        }
        chart.update("none");
      });
    }).observe(document.documentElement, {attributes: true, attributeFilter: ["data-bs-theme"]});
  }
});
