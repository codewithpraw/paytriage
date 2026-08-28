Paytriage — An AI agent that intelligently detects and recovers failed recurring payments using Claude AI. Built for Razorpay's AI Buildathon.

# Paytriage

An intelligent AI agent that detects and recovers failed recurring payments using Claude AI.

## The Problem

Merchants lose significant revenue to failed recurring payments due to card expirations, insufficient funds, network issues, and bank declines. Traditional retry systems apply one-size-fits-all logic. Paytriage uses AI to intelligently triage each failure and execute targeted recovery strategies.

## The Solution

Paytriage is an AI-powered recovery agent that:
- **Detects** failed recurring payments across merchant accounts
- **Analyzes** root causes (card expired vs insufficient funds vs network error)
- **Triages** which payments are recoverable and worth pursuing
- **Executes** targeted recovery workflows (immediate retry vs SMS notification then retry vs customer contact)
- **Measures** results with detailed audit trails and recovery metrics

## Key Features

✅ AI-powered root cause analysis using Claude AI  
✅ Intelligent retry strategy selection (not all failures warrant retry)  
✅ Bounded, explainable decisions with audit trails  
✅ Measured results: amount recovered, success rate, failure reasons  
✅ Compliance-first: escalation rules, stopping conditions, no spam  

## Results

Recovers lost revenue from failed recurring payments with measured precision and recall.

**Example Metrics:**
- Detected: 50 failed payments (₹25,000 total)
- Recovered: 35 payments (₹17,500 total) 
- Success Rate: 70%
- Average Recovery Time: 2-3 days

## Built For

Razorpay AI Buildathon — AI Revenue Recovery Track
