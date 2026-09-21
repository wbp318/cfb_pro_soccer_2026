# The NHL prop analysis loop: is the projection any good, and (once the ledger fills) does
# betting it against posted lines make money?
#
#   A. Paper ROI by market x side x strength, flat $1, bootstrap 95% CI (empty pre-season).
#   B. Slices: edge band, market, side (Wilson CI on hit, flat ROI).
#   C. Walk-forward projection calibration on the stored game logs (same recipe as
#      nhl_edge.calibrate): log-loss vs naive league-average + reliability bins.
#
# Output: analysis/_out/nhl_*.csv. Mirrors nhl_loop.py — keep in lockstep.

suppressPackageStartupMessages({
  library(dplyr)
  library(boot)
})

.this_dir <- tryCatch(dirname(sys.frame(1)$ofile), error = function(e) "analysis/06_nhl")
source(file.path(.this_dir, "..", "_shared", "load_data.R"))     # wilson()
source(file.path(.this_dir, "..", "_shared", "load_nhl.R"))

OUT_DIR   <- normalizePath(file.path(.this_dir, "..", "_out"), mustWork = FALSE)
MIN_BETS  <- 10
MIN_N     <- 5
BOOT_REPS <- 5000
SEED      <- 20261007

PRIOR_SEASON  <- 20252026
SHRINK_GAMES  <- 20
RECENT_GAMES  <- 10
RECENT_WEIGHT <- 0.35
MIN_PRIOR     <- 10
LINES <- c(shots = 2.5, points = 0.5, saves = 27.5)
EDGE_BINS   <- c(0, 8, 15, 30, 50, 100000)
EDGE_LABELS <- c("0-8", "8-15", "15-30", "30-50", "50+")

dir.create(OUT_DIR, showWarnings = FALSE, recursive = TRUE)
set.seed(SEED)

verdict <- function(lo, hi) if (lo > 0) "PROFITABLE (95% CI > 0)" else if (hi < 0) "losing (95% CI < 0)" else "inconclusive"
boot_ci <- function(x) {
  b <- boot(x, function(d, i) mean(d[i]), R = BOOT_REPS)
  ci <- boot.ci(b, type = "perc")$percent
  c(lo = ci[4], hi = ci[5])
}
p_over <- function(lam, line) 1 - ppois(floor(line), lam)

# ---------------------------------------------------------------- A + B
section_a <- function(bets) {
  cat(sprintf("A. Paper ROI — %s settled NHL paper props, flat ROI %+.1f%%\n\n",
              format(nrow(bets), big.mark = ","), 100 * mean(bets$pnl_flat)))
  groups <- list(list(market = "ALL", side = "all", strength = "all", g = bets))
  for (key in split(bets, list(bets$market, bets$side, bets$strength), drop = TRUE))
    groups[[length(groups) + 1]] <- list(market = key$market[1], side = key$side[1], strength = as.character(key$strength[1]), g = key)
  for (key in split(bets, bets$market))
    groups[[length(groups) + 1]] <- list(market = key$market[1], side = "any", strength = "any", g = key)
  for (key in split(bets, bets$side))
    groups[[length(groups) + 1]] <- list(market = "ALL", side = key$side[1], strength = "any", g = key)
  rows <- lapply(groups, function(x) {
    g <- x$g
    if (nrow(g) < MIN_BETS) return(NULL)
    ci <- boot_ci(g$pnl_flat)
    data.frame(market = x$market, side = x$side, strength = x$strength, bets = nrow(g), wins = sum(g$result == "W"),
               hit_rate = mean(g$result == "W"), roi_mean = mean(g$pnl_flat), roi_ci_lo = ci[["lo"]],
               roi_ci_hi = ci[["hi"]], verdict = verdict(ci[["lo"]], ci[["hi"]]), stringsAsFactors = FALSE)
  })
  out <- bind_rows(rows) %>% arrange(desc(roi_ci_lo))
  cat(sprintf("  %-26s%-6s%4s%6s%6s%7s%8s%8s%8s  verdict\n", "market", "side", "str", "bets", "wins", "hit%", "ROI", "CI lo", "CI hi"))
  for (i in seq_len(nrow(out))) {
    r <- out[i, ]
    cat(sprintf("  %-26s%-6s%4s%6d%6d%6.1f%%%+7.1f%%%+7.1f%%%+7.1f%%  %s\n", r$market, r$side, r$strength, r$bets, r$wins,
                100 * r$hit_rate, 100 * r$roi_mean, 100 * r$roi_ci_lo, 100 * r$roi_ci_hi, r$verdict))
  }
  out
}

slice_table <- function(df, col, order, title) {
  cat(sprintf("\n%s\n", title))
  cat(sprintf("  %-26s%5s%5s%8s%8s   95%% CI on hit\n", "bucket", "n", "W", "hit%", "ROI"))
  rows <- list()
  for (key in order[order %in% unique(df[[col]])]) {
    g <- df[df[[col]] == key, ]
    if (nrow(g) < MIN_N) next
    w <- sum(g$result == "W"); wi <- wilson(w, nrow(g) - sum(g$result == "P"))
    rows[[length(rows) + 1]] <- data.frame(dimension = col, bucket = key, n = nrow(g), wins = w, hit_rate = wi[1],
                                           hit_lo = wi[2], hit_hi = wi[3], roi_flat = mean(g$pnl_flat), stringsAsFactors = FALSE)
    cat(sprintf("  %-26s%5d%5d%7.1f%%%+7.1f%%   [%.0f, %.0f]\n", key, nrow(g), w, 100 * wi[1], 100 * mean(g$pnl_flat),
                100 * wi[2], 100 * wi[3]))
  }
  rows
}

section_b <- function(bets) {
  b <- bets[bets$strength >= 1, ]
  b$edge_band <- as.character(cut(b$edge, EDGE_BINS, labels = EDGE_LABELS, right = FALSE))
  rows <- slice_table(b, "edge_band", EDGE_LABELS, "B1. by edge band (does a bigger projection-vs-line gap win more?)")
  rows <- c(rows, slice_table(b, "market", sort(unique(b$market)), "B2. by market"))
  rows <- c(rows, slice_table(b, "side", c("over", "under"), "B3. by side"))
  bind_rows(rows)
}

# ---------------------------------------------------------------- C
section_c <- function(logs) {
  rows <- list()
  for (stat in names(LINES)) {
    line <- LINES[[stat]]
    d <- if (stat == "saves") logs[!is.na(logs$started) & logs$started == 1 & !is.na(logs$saves), ] else
      logs[!is.na(logs$toi) & !is.na(logs[[stat]]), ]
    if (nrow(d) == 0) next
    lg <- mean(d[[stat]]); pn <- p_over(lg, line)
    bins <- list(); ll_m <- 0; ll_n <- 0; n <- 0
    eps <- 1e-6
    for (vs in split(d[[stat]], d$player_id)) {           # split() keeps within-player row order
      vs <- as.numeric(vs)
      if (length(vs) <= MIN_PRIOR) next
      for (i in (MIN_PRIOR + 1):length(vs)) {
        hist <- vs[1:(i - 1)]
        w <- length(hist) / (length(hist) + SHRINK_GAMES)
        lam <- w * mean(hist) + (1 - w) * lg
        rv <- tail(hist, RECENT_GAMES)
        if (length(rv) >= 5) lam <- (1 - RECENT_WEIGHT) * lam + RECENT_WEIGHT * mean(rv)
        po <- p_over(lam, line)
        y <- if (vs[i] > line) 1 else 0
        ll_m <- ll_m - (y * log(max(po, eps)) + (1 - y) * log(max(1 - po, eps)))
        ll_n <- ll_n - (y * log(max(pn, eps)) + (1 - y) * log(max(1 - pn, eps)))
        b <- as.character(min(9, floor(po * 10)))
        if (is.null(bins[[b]])) bins[[b]] <- c(0, 0, 0)
        bins[[b]] <- bins[[b]] + c(1, po, y)
        n <- n + 1
      }
    }
    if (n == 0) next
    cat(sprintf("\nC. %s over %g — %s player-games (%d), walk-forward, no prior season\n", toupper(stat), line,
                format(n, big.mark = ","), PRIOR_SEASON))
    cat(sprintf("  log-loss: model %.4f · naive league-average %.4f → %s by %.4f\n", ll_m / n, ll_n / n,
                if (ll_m < ll_n) "model better" else "NAIVE better", abs(ll_m - ll_n) / n))
    cat(sprintf("  %-12s%7s%8s%8s%7s\n", "P(over) bin", "n", "pred", "obs", "Δpp"))
    for (b in as.character(sort(as.integer(names(bins))))) {
      v <- bins[[b]]; bi <- as.integer(b)
      cat(sprintf("  %.1f-%.1f     %7d%7.1f%%%7.1f%%%+6.1f\n", bi / 10, (bi + 1) / 10, v[1], 100 * v[2] / v[1],
                  100 * v[3] / v[1], 100 * (v[3] - v[2]) / v[1]))
      rows[[length(rows) + 1]] <- data.frame(stat = stat, line = line, bin = sprintf("%.1f-%.1f", bi / 10, (bi + 1) / 10),
                                             n = v[1], pred = v[2] / v[1], obs = v[3] / v[1], stringsAsFactors = FALSE)
    }
    rows[[length(rows) + 1]] <- data.frame(stat = stat, line = line, bin = "logloss", n = n, pred = ll_m / n, obs = ll_n / n,
                                           stringsAsFactors = FALSE)
  }
  bind_rows(rows)
}

bets <- load_nhl_bets()
if (nrow(bets) == 0) {
  cat("no settled NHL paper props yet (season opens 2026-10-07) — skipping A and B\n")
} else {
  write.csv(section_a(bets), file.path(OUT_DIR, "nhl_roi.csv"), row.names = FALSE)
  write.csv(section_b(bets), file.path(OUT_DIR, "nhl_slices.csv"), row.names = FALSE)
}
logs <- load_game_logs(PRIOR_SEASON)
if (nrow(logs) == 0) {
  cat("no game logs in nhl.db — run nhl_edge.py --build\n")
  quit(status = 0)
}
write.csv(section_c(logs), file.path(OUT_DIR, "nhl_calibration.csv"), row.names = FALSE)
cat(sprintf("\nwrote %s\n", file.path(OUT_DIR, "nhl_*.csv")))
