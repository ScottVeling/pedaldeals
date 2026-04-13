var activeCategory = "all";
var searchQuery = "";
var sortBy = "discount";

var grid = document.getElementById("dealsGrid");
var countEl = document.getElementById("dealCount");
var noResults = document.getElementById("noResults");
var searchInput = document.getElementById("searchInput");
var sortSelect = document.getElementById("sortSelect");
var filterBtns = document.querySelectorAll(".cat");

function pct(d) {
  return Math.round((1 - d.priceNow / d.priceWas) * 100);
}

function ago(dateStr) {
  var diff = Date.now() - new Date(dateStr).getTime();
  var days = Math.floor(diff / 86400000);
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  return days + "d ago";
}

function eur(n) {
  return "\u20AC" + n.toLocaleString("nl-NL");
}

var catLabels = {
  bikes: "bike",
  parts: "component",
  accessories: "accessory",
  clothing: "clothing",
  tools: "tools"
};

function render() {
  var filtered = DEALS.filter(function(d) {
    var matchCat = activeCategory === "all" || d.category === activeCategory;
    var matchSearch = !searchQuery ||
      d.title.toLowerCase().indexOf(searchQuery) !== -1 ||
      d.store.toLowerCase().indexOf(searchQuery) !== -1 ||
      d.category.toLowerCase().indexOf(searchQuery) !== -1;
    return matchCat && matchSearch;
  });

  filtered.sort(function(a, b) {
    if (sortBy === "discount") return pct(b) - pct(a);
    if (sortBy === "price-low") return a.priceNow - b.priceNow;
    if (sortBy === "price-high") return b.priceNow - a.priceNow;
    if (sortBy === "newest") return new Date(b.added) - new Date(a.added);
    return 0;
  });

  countEl.textContent = filtered.length;
  noResults.style.display = filtered.length === 0 ? "block" : "none";

  var html = "";
  for (var i = 0; i < filtered.length; i++) {
    var d = filtered[i];
    var discount = pct(d);
    var pickClass = d.pick ? " pick" : "";

    html += '<article class="card' + pickClass + '">' +
      '<div class="card-top">' +
        '<span class="card-store">' + d.store + '</span>' +
        '<span class="card-badge">' + discount + '% off</span>' +
      '</div>' +
      '<span class="card-cat">' + (catLabels[d.category] || d.category) + '</span>' +
      '<h3 class="card-title">' + d.title + '</h3>' +
      '<div class="card-prices">' +
        '<span class="price-now">' + eur(d.priceNow) + '</span>' +
        '<span class="price-was">' + eur(d.priceWas) + '</span>' +
      '</div>' +
      '<div class="card-bottom">' +
        '<span class="card-date">' + ago(d.added) + '</span>' +
        '<a href="' + d.storeUrl + '" class="card-link" target="_blank" rel="noopener">go to shop &rarr;</a>' +
      '</div>' +
    '</article>';

    if ((i + 1) % 8 === 0 && i + 1 < filtered.length) {
      html += '<div class="ad-card">ad</div>';
    }
  }

  grid.innerHTML = html;
}

// Filters
filterBtns.forEach(function(btn) {
  btn.addEventListener("click", function() {
    filterBtns.forEach(function(b) { b.classList.remove("active"); });
    btn.classList.add("active");
    activeCategory = btn.dataset.category;
    render();
  });
});

searchInput.addEventListener("input", function(e) {
  searchQuery = e.target.value.toLowerCase().trim();
  render();
});

sortSelect.addEventListener("change", function(e) {
  sortBy = e.target.value;
  render();
});

// Submit form (placeholder — just shows a thank you)
var submitForm = document.getElementById("submitForm");
if (submitForm) {
  submitForm.addEventListener("submit", function(e) {
    e.preventDefault();
    submitForm.innerHTML = '<p style="color:#2a7d4f;font-size:0.9rem;">Thanks! We\'ll check it out and add it if it\'s good.</p>';
  });
}

// Newsletter form (placeholder)
var nlForm = document.getElementById("newsletterForm");
if (nlForm) {
  nlForm.addEventListener("submit", function(e) {
    e.preventDefault();
    nlForm.innerHTML = '<p style="color:#2a7d4f;font-size:0.9rem;">You\'re in. First email next Sunday.</p>';
  });
}

render();
