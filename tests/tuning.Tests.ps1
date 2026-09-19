# Pester 3.4 tests for the speed/accuracy tuning helpers in scripts\lib.ps1 and eval.ps1 -Compare. No server needed.
. "$PSScriptRoot\..\scripts\lib.ps1"

Describe 'Test-ModelFile' {
    $dir = Join-Path $env:TEMP "llm-model-test-$PID"; New-Item -ItemType Directory -Force $dir | Out-Null
    $model = "$dir\m.gguf"; [IO.File]::WriteAllBytes($model, (New-Object byte[] 10)); $exp = "$dir\expected-sha256.txt"

    It 'passes a file whose size matches the expected list' {
        Set-Content $exp "m.gguf 10 abc`nother.gguf 5 def"
        Test-ModelFile $model $exp | Should Be $null
    }
    It 'rejects a truncated file with its actual and expected size' {
        Set-Content $exp 'm.gguf 16817244384 abc'
        Test-ModelFile $model $exp | Should Match '10 bytes, expected 16817244384'
    }
    It 'passes models not in the list and a missing list' {
        Set-Content $exp 'other.gguf 5 def'
        Test-ModelFile $model $exp | Should Be $null
        Test-ModelFile $model "$dir\none.txt" | Should Be $null
    }
    It 'reports a missing model' { Test-ModelFile "$dir\absent.gguf" $exp | Should Match '^Not found' }
    Remove-Item -Recurse -Force $dir
}

Describe 'Get-SignTestP' {
    It 'is 1 with no discordant pairs' { Get-SignTestP 0 0 | Should Be 1 }
    It 'is 1 for a tie' { Get-SignTestP 3 3 | Should Be 1 }
    It 'matches the exact binomial for 0 vs 6' { Get-SignTestP 0 6 | Should Be 0.03125 }
    It 'matches the exact binomial for 8 vs 1 and is symmetric' {
        Get-SignTestP 8 1 | Should Be 0.0390625
        Get-SignTestP 1 8 | Should Be 0.0390625
    }
    It 'does not call one flipped answer a difference' { Get-SignTestP 0 1 | Should Be 1 }
}

Describe 'Test-Escalate' {
    $p = "How many minutes? End with 'Answer: <number>'."
    It 'escalates when a required Answer line is missing' { Test-Escalate $p 'It takes about five minutes.' $false | Should Be $true }
    It 'does not escalate when the Answer line is present' { Test-Escalate $p "Work...`nAnswer: 159" $false | Should Be $false }
    It 'escalates when the schema is still invalid' { Test-Escalate 'Classify this.' 'not json' $true | Should Be $true }
    It 'does not escalate free-form prompts' { Test-Escalate 'Tell me a joke.' 'A joke.' $false | Should Be $false }
}

Describe 'Format-LongContextPrompt' {
    $src = Join-Path $env:TEMP "llm-filler-$PID.md"
    Set-Content $src ((1..40 | ForEach-Object { "Paragraph $_ " + ('filler text ' * 20) }) -join "`r`n`r`n")
    $needle = 'NEEDLE: the code is 7341.'

    It 'pads to at least the requested size and ends with the question' {
        $p = Format-LongContextPrompt $needle 'What is the code?' 2000 0.5 @($src)
        $p.Length | Should Not BeLessThan 8000
        $p | Should Match 'Question: What is the code\?$'
    }
    It 'places the needle near the requested fraction (not rounded to the start)' {
        foreach ($at in 0.1, 0.5, 0.9) {
            $p = Format-LongContextPrompt $needle 'Q?' 3000 $at @($src)
            [math]::Abs($p.IndexOf($needle) / $p.Length - $at) | Should BeLessThan 0.1
        }
    }
    It 'is deterministic' { Format-LongContextPrompt $needle 'Q?' 1000 0.3 @($src) | Should Be (Format-LongContextPrompt $needle 'Q?' 1000 0.3 @($src)) }
    Remove-Item $src
}

Describe 'Invoke-Ask escalation' {
    $prof = [pscustomobject]@{ Profile = 'test'; Port = 1; ThinkThreshold = 99; RagK = 2; RagTokens = 800 }
    $reply = { param($Body) $think = $Body.chat_template_kwargs.enable_thinking
        [pscustomobject]@{ choices = @([pscustomobject]@{ message = [pscustomobject]@{ content = $(if ($think) { 'Answer: 5' } else { 'five' }); reasoning_content = '' } })
                           timings = [pscustomobject]@{ prompt_n = 10; prompt_ms = 100; predicted_n = 4; predicted_per_second = 5; draft_n = 0; draft_n_accepted = 0 } } }
    Mock Invoke-Chat $reply

    It 're-asks once with thinking when the router skipped it and the answer is unparseable' {
        $r = Invoke-Ask -Prompt "Pick 5. End with 'Answer: <number>'." -ServerProfile $prof
        $r.Escalated | Should Be $true
        $r.Content | Should Be 'Answer: 5'
        $r.Calls | Should Be 2
        $r.PromptTokens | Should Be 20
        $r.ThinkReasons -contains 'escalated' | Should Be $true
    }
    It 'never escalates when thinking is forced off' {
        $r = Invoke-Ask -Prompt "Pick 5. End with 'Answer: <number>'." -ThinkMode off -ServerProfile $prof
        $r.Escalated | Should Be $false
        $r.Calls | Should Be 1
    }
    It 'never escalates a /nothink prompt' {
        (Invoke-Ask -Prompt "/nothink Pick 5. End with 'Answer: <number>'." -ServerProfile $prof).Escalated | Should Be $false
    }
}

Describe 'eval.ps1 -Compare paired sign test' {
    $dir = Join-Path $env:TEMP "llm-eval-test-$PID"; New-Item -ItemType Directory -Force $dir | Out-Null
    function Write-Run([string]$Path, [bool[]]$Pass) {
        $res = @(for ($i = 0; $i -lt $Pass.Count; $i++) { @{ id = "q$i"; category = 'math'; rep = 1; pass = $Pass[$i]; prompt_ms = 1; wall_ms = 1; gen_tps = 1 } })
        @{ meta = @{ profile = 'p'; think = 'auto' }; summary = @(@{ category = 'math'; acc = 0; med_wall_s = 1; med_tps = 1 }); results = $res } | ConvertTo-Json -Depth 5 | Set-Content $Path -Encoding UTF8
    }

    It 'reports B better when B wins 6 discordant pairs and A none' {
        Write-Run "$dir\a.json" @($false, $false, $false, $false, $false, $false, $true, $true)
        Write-Run "$dir\b.json" @($true, $true, $true, $true, $true, $true, $true, $true)
        $out = & "$PSScriptRoot\..\scripts\eval.ps1" -Compare "$dir\a.json", "$dir\b.json" 6>$null | Out-String
        $out | Should Match 'B better'
        $out | Should Match '0\.031'
    }
    It 'reports no evidence for a single flipped answer' {
        Write-Run "$dir\a.json" @($false, $true, $true)
        Write-Run "$dir\b.json" @($true, $true, $true)
        & "$PSScriptRoot\..\scripts\eval.ps1" -Compare "$dir\a.json", "$dir\b.json" 6>$null | Out-String | Should Match 'no evidence'
    }
    Remove-Item -Recurse -Force $dir
}
