@{
    Severity     = @('Error', 'Warning')
    # Scripts are interactive CLI tools: coloured Write-Host output is intended, not a defect.
    ExcludeRules = @('PSAvoidUsingWriteHost')
}
