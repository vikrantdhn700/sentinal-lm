param(
    [string]$ReportPath = (Join-Path $PSScriptRoot '../reports/atlas-results.json'),
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '../screenshots')
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$report = Get-Content -LiteralPath $ReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

function Save-OutputImage {
    param([string]$Name, [string]$Title, [string[]]$Lines)
    $font = New-Object System.Drawing.Font('Consolas', 17)
    $titleFont = New-Object System.Drawing.Font('Segoe UI', 24, [System.Drawing.FontStyle]::Bold)
    $smallFont = New-Object System.Drawing.Font('Consolas', 12)
    $height = 150 + 30 * $Lines.Count
    $bitmap = New-Object System.Drawing.Bitmap(1520, $height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $graphics.Clear([System.Drawing.Color]::FromArgb(15, 23, 42))
    $foreground = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(226, 232, 240))
    $accent = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(94, 234, 212))
    $muted = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(148, 163, 184))
    try {
        $graphics.DrawString($Title, $titleFont, $accent, 32, 22)
        $graphics.DrawString('SENTINEL | Recorded Atlas results | ' + $report.generated_at_utc, $smallFont, $muted, 34, 72)
        $position = 122
        foreach ($line in $Lines) {
            $graphics.DrawString($line, $font, $foreground, 34, $position)
            $position += 30
        }
        $bitmap.Save((Join-Path $OutputDirectory $Name), [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $foreground.Dispose(); $accent.Dispose(); $muted.Dispose()
        $font.Dispose(); $titleFont.Dispose(); $smallFont.Dispose()
        $graphics.Dispose(); $bitmap.Dispose()
    }
}

$crudLines = @(
    'Connected to MongoDB (ping successful).',
    ('Upserted {0:N0} customers and {1:N0} transactions.' -f $report.load.sampled_customers, $report.load.sampled_transactions),
    '',
    ('insert_one: {0} document' -f $report.crud.insert_one),
    ('insert_many: {0} documents with separate generated _id values' -f $report.crud.insert_many),
    'find filter: customer_id=DEMO_CUSTOMER, transaction_amount >= 20',
    'projection: transaction_id, transaction_amount, merchant_category; _id excluded',
    ''
)
foreach ($row in $report.crud.find) {
    $crudLines += ('  {0} | INR {1,7:N2} | {2}' -f $row.transaction_id, $row.transaction_amount, $row.merchant_category)
}
$crudLines += @(
    '',
    ('update_many: matched={0}, modified={1}' -f $report.crud.update_many_matched, $report.crud.update_many_modified),
    ('delete_one: deleted={0}' -f $report.crud.delete_one),
    ('Final collection counts: customers={0}, transactions={1}' -f $report.counts.customers, $report.counts.transactions),
    ('Remaining demo transactions: {0}' -f $report.remaining_demo_transactions)
)
Save-OutputImage '01-load-and-crud.png' 'Connection, loading and CRUD' $crudLines

$before = $report.explain.before
$after = $report.explain.after
Save-OutputImage '02-explain.png' 'Same query: collection scan and indexed scan' @(
    ('customer_id = ' + $report.explain.query.customer_id),
    ('transaction_time >= ' + $report.explain.query.transaction_time.'$gte'),
    ('transaction_time <= ' + $report.explain.query.transaction_time.'$lte'),
    '',
    'BEFORE: hint([("$natural", 1)]).explain()',
    ('winningPlan.stage: {0}' -f $before.winningPlan.stage),
    ('nReturned={0}, totalDocsExamined={1}, totalKeysExamined={2}' -f $before.nReturned, $before.totalDocsExamined, $before.totalKeysExamined),
    '',
    'AFTER: hint("customer_time_idx").explain()',
    ('winningPlan: {0} -> {1}' -f $after.winningPlan.stage, $after.winningPlan.inputStage.stage),
    ('indexName: {0}' -f $after.winningPlan.inputStage.indexName),
    ('nReturned={0}, totalDocsExamined={1}, totalKeysExamined={2}' -f $after.nReturned, $after.totalDocsExamined, $after.totalKeysExamined),
    '',
    'Hints deliberately control the comparison; existing indexes are preserved.'
)

$fraudLines = @('merchant_category       total    fraud    fraud_rate', '--------------------------------------------------')
foreach ($row in $report.aggregations.fraud_by_category) {
    $fraudLines += ('{0,-21} {1,7} {2,8}    {3:P4}' -f $row._id, $row.total_transactions, $row.fraud_transactions, $row.fraud_rate)
}
$fraudLines += @('', 'fraud_rate = fraud_transactions / labelled transactions in that category', 'Demo records excluded. Labels joined from SENTINEL source labels, not predictions.')
Save-OutputImage '03-fraud-rate.png' 'Aggregation 1: fraud rate per merchant category' $fraudLines

$topLines = @('customer_id             count       total INR    last activity (UTC)', '----------------------------------------------------------------------------------')
foreach ($row in $report.aggregations.top_customers_last_24h) {
    $timestamp = [datetimeoffset]::Parse($row.last_activity).ToString('yyyy-MM-dd HH:mm:ss')
    $topLines += ('{0,-22} {1,5} {2,15:N2}    {3}' -f $row._id, $row.transaction_count, $row.total_amount_inr, $timestamp)
}
$topLines += @('', 'Each customer has their own window: [last_activity - 24 hours, last_activity].', 'Totals are summed in integer paise before conversion to INR. Demo rows excluded.')
Save-OutputImage '04-top-customers.png' 'Aggregation 2: top 10 customers in their last 24 hours' $topLines
Write-Output "Rendered four output images from $ReportPath"
