# Where exactly does the paper ledger win and lose? Flat-bet hit rate + ROI by the
# dimensions a play rule can act on: edge band, ML price band, |spread|, dog/fav,
# home/away, and the model's own truth_p vs what actually happened (calibration).
#
# This is the script that motivated the 2026-09-20 rule changes (SPREAD_OVERREACH_PTS,
# ML_DEAD_ZONE, LIVE_STAKES=False). Wilson 95% intervals on hit rate; ROI is flat $1.
#
# Output: analysis/_out/deep_dive.csv + console tables.
# Mirrors deep_dive.py — keep them in lockstep. Point estimates must match exactly.

suppressPackageStartupMessages(library(dplyr))

.this_dir <- tryCatch(dirname(sys.frame(1)$ofile), error = function(e) "analysis/04_deep_dive")
source(file.path(.this_dir, "..", "_shared", "load_data.R"))

OUT_DIR <- normalizePath(file.path(.this_dir, "..", "_out"), mustWork = FALSE)
OUT_CSV <- file.path(OUT_DIR, "deep_dive.csv")
MIN_N   <- 5

SPREAD_EDGE_BINS   <- c(0, 3, 5, 8, 1000)
SPREAD_EDGE_LABELS <- c("0-3", "3-5", "5-8", "8+")
ABS_SPREAD_BINS    <- c(-1, 3, 7, 14, 21, 28, 1000)
ABS_SPREAD_LABELS  <- c("0-3", "3-7", "7-14", "14-21", "21-28", "28+")
ML_PRICE_BINS      <- c(-100000, -200, -110, 99, 150, 200, 250, 100000)   # right-closed: +100..150 inclusive = ML_DEAD_ZONE
ML_PRICE_LABELS    <- c("<-200", "-200..-110", "-110..+100", "+100..150", "+150..200", "+200..250", ">+250")
ML_EDGE_BINS       <- c(0, 8, 15, 20, 30, 50, 100000)
ML_EDGE_LABELS     <- c("0-8", "8-15", "15-20", "20-30", "30-50", "50+")
TRUTH_BINS         <- c(0, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0)
TRUTH_LABELS       <- c("<.4", ".4-.5", ".5-.6", ".6-.7", ".7-.8", ".8+")

dir.create(OUT_DIR, showWarnings = FALSE, recursive = TRUE)

# pandas.cut(right=False) == cut(right = FALSE); the price band is right-closed on purpose.
cut_lo <- function(x, bins, labels) as.character(cut(x, bins, labels = labels, right = FALSE))

b <- load_paper_bets()
if (nrow(b) == 0) {
  cat("no settled paper bets yet — run cfb_edge.py --snapshot / --settle (or --backfill)\n")
  quit(status = 0)
}
g <- load_games(fbs_only = FALSE)[, c("game_id", "home", "away", "home_spread", "home_fpi", "away_fpi")]
b <- merge(b, g, by = "game_id", all.x = TRUE)
b <- b[b$strength >= 1 & !is.na(b$home_fpi) & !is.na(b$away_fpi), ]
b$season     <- substr(b$date, 1, 4)
b$side_home  <- ifelse(b$side == b$home, "home", "away")
b$truth_band <- cut_lo(b$truth_p, TRUTH_BINS, TRUTH_LABELS)
sp <- b$kind == "spread"
ml <- b$kind == "ml"
b$edge_band  <- NA_character_
b$abs_spread <- NA_character_
b$dog_fav    <- NA_character_
b$price_band <- NA_character_
b$edge_band[sp]  <- cut_lo(b$edge[sp], SPREAD_EDGE_BINS, SPREAD_EDGE_LABELS)
b$abs_spread[sp] <- cut_lo(abs(b$home_spread[sp]), ABS_SPREAD_BINS, ABS_SPREAD_LABELS)
side_is_home     <- b$side == b$home
b$dog_fav[sp]    <- ifelse(((b$home_spread > 0) == side_is_home)[sp], "dog", "fav")
b$price_band[ml] <- as.character(cut(b$price[ml], ML_PRICE_BINS, labels = ML_PRICE_LABELS, right = TRUE))
b$edge_band[ml]  <- cut_lo(b$edge[ml], ML_EDGE_BINS, ML_EDGE_LABELS)

ORDER <- list(edge_band = c(SPREAD_EDGE_LABELS, ML_EDGE_LABELS), abs_spread = ABS_SPREAD_LABELS,
              price_band = ML_PRICE_LABELS, truth_band = TRUTH_LABELS)

table_by <- function(df, by, kind, question) {
  keys <- unique(as.character(df[[by]]))
  keys <- if (!is.null(ORDER[[by]])) ORDER[[by]][ORDER[[by]] %in% keys] else sort(keys)
  rows <- list()
  cat(sprintf("\n%s\n", question))
  cat(sprintf("  %-12s%5s%5s%8s%8s   95%% CI on hit\n", "bucket", "n", "W", "hit%", "ROI"))
  for (k in keys) {
    grp <- df[as.character(df[[by]]) == k, ]
    n <- nrow(grp)
    if (n < MIN_N) next
    w <- sum(grp$result == "W")
    dec <- n - sum(grp$result == "P")
    wi <- wilson(w, dec)
    r <- data.frame(kind = kind, dimension = by, bucket = k, n = n, wins = w,
                    hit_rate = wi[1], hit_lo = wi[2], hit_hi = wi[3], roi_flat = mean(grp$pnl_flat),
                    stringsAsFactors = FALSE)
    flag <- if (kind == "spread" && r$hit_hi < BREAK_EVEN_110) "  losing (CI hi < BE)" else ""
    cat(sprintf("  %-12s%5d%5d%7.1f%%%+7.1f%%   [%.0f, %.0f]%s\n", k, n, w, 100 * r$hit_rate,
                100 * r$roi_flat, 100 * r$hit_lo, 100 * r$hit_hi, flag))
    rows[[length(rows) + 1]] <- r
  }
  rows
}

rows <- list()
for (kind in c("spread", "ml")) {
  d <- b[b$kind == kind, ]
  cat(sprintf("\n=== %s — %d settled flagged paper bets (FBS vs FBS, strength ≥ 1); break-even at -110 = %.1f%%\n",
              toupper(kind), nrow(d), 100 * BREAK_EVEN_110))
  rows <- c(rows, table_by(d, "strength", kind, "A. by strength (1 = lean/value, 2 = STRONG)"))
  rows <- c(rows, table_by(d, "season", kind, "B. by season"))
  rows <- c(rows, table_by(d, "edge_band", kind, "C. by edge band (does a bigger FPI-vs-DK gap win more?)"))
  if (kind == "spread") {
    rows <- c(rows, table_by(d, "abs_spread", kind, "D. by |spread|"))
    rows <- c(rows, table_by(d, "dog_fav", kind, "E. FPI side is the dog or the favourite"))
  } else {
    rows <- c(rows, table_by(d, "price_band", kind, "D. by moneyline price band"))
  }
  rows <- c(rows, table_by(d, "side_home", kind, "F. home vs away"))
  rows <- c(rows, table_by(d, "truth_band", kind, "G. model truth_p vs actual hit rate (calibration)"))
}
out <- bind_rows(rows)
write.csv(out, OUT_CSV, row.names = FALSE)
cat(sprintf("\nwrote %s\n", OUT_CSV))
