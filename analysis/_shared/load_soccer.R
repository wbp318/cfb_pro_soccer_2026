# Shared loader for the soccer half of analysis/ (reads soccer.db, never writes).
#
#   source("analysis/_shared/load_soccer.R")
#   bets    <- load_soccer_bets()      # one row per settled soccer paper bet (3-way)
#   matches <- load_soccer_matches()   # one row per settled match w/ last snapshot (closer + Elo probs)
#   results <- load_results()          # every stored final score, chronological (for Elo refits)
#
# Mirrors load_soccer.py — keep them in lockstep. CFB_SOCCER_DB overrides the path (CI).

suppressPackageStartupMessages({
  library(DBI)
  library(RSQLite)
  library(dplyr)
})

.soccer_shared_dir <- tryCatch(dirname(sys.frame(1)$ofile), error = function(e) "analysis/_shared")
SOCCER_DB <- if (nzchar(Sys.getenv("CFB_SOCCER_DB"))) Sys.getenv("CFB_SOCCER_DB") else
  normalizePath(file.path(.soccer_shared_dir, "..", "..", "soccer.db"), mustWork = FALSE)

.MATCHES_SQL <- "
WITH last AS (
    SELECT s.*, ROW_NUMBER() OVER (PARTITION BY match_id ORDER BY taken_at DESC) AS rn
    FROM snapshots s
    WHERE home_ml IS NOT NULL AND draw_ml IS NOT NULL AND away_ml IS NOT NULL
)
SELECT m.id AS match_id, m.date, m.league, m.league_name, m.neutral, m.home, m.away,
       m.home_score, m.away_score,
       last.home_ml, last.draw_ml, last.away_ml, last.home_ml_open, last.draw_ml_open, last.away_ml_open,
       last.total, last.total_open, last.home_elo, last.away_elo, last.home_elo_n, last.away_elo_n,
       last.home_p, last.draw_p, last.away_p, last.backfill
FROM matches m
JOIN last ON last.match_id = m.id AND last.rn = 1
WHERE m.completed = 1 AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
"

.SBETS_SQL <- "
SELECT p.id, p.match_id, m.date, m.league, p.kind, p.pick, p.side, p.price, p.truth_p, p.edge,
       p.strength, p.stake, p.result, p.profit, p.backfill
FROM paper_bets p JOIN matches m ON m.id = p.match_id
WHERE p.result IS NOT NULL
"

.RESULTS_SQL <- "
SELECT id, date, league, home_id, away_id, home_score, away_score, neutral
FROM results ORDER BY date, id
"

.squery <- function(sql, db_path) {
  con <- dbConnect(SQLite(), db_path)
  on.exit(dbDisconnect(con))
  dbGetQuery(con, sql)
}

.sdec <- function(price) ifelse(price > 0, 1 + price / 100, 1 + 100 / abs(price))
.simplied <- function(price) 1 / .sdec(price)

load_soccer_bets <- function(db_path = SOCCER_DB) {
  df <- .squery(.SBETS_SQL, db_path)
  df$pnl_flat <- ifelse(df$result == "W", .sdec(df$price) - 1, ifelse(df$result == "L", -1, 0))
  df
}

load_soccer_matches <- function(db_path = SOCCER_DB, rated_only = TRUE) {
  df <- .squery(.MATCHES_SQL, db_path)
  if (rated_only) df <- df[!is.na(df$home_p), ]
  ih <- .simplied(df$home_ml); id <- .simplied(df$draw_ml); ia <- .simplied(df$away_ml)
  s <- ih + id + ia
  df$fair_home <- ih / s; df$fair_draw <- id / s; df$fair_away <- ia / s
  df$outcome <- ifelse(df$home_score > df$away_score, "home",
                ifelse(df$home_score < df$away_score, "away", "draw"))
  rownames(df) <- NULL
  df
}

load_results <- function(db_path = SOCCER_DB) .squery(.RESULTS_SQL, db_path)
