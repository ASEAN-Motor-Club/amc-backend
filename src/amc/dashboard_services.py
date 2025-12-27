from datetime import timedelta
from django.utils import timezone
from django.db.models import Sum, Count, F, Max
from django.db.models.functions import TruncDay
from amc_finance.models import Account, LedgerEntry
from amc.models import PlayerStatusLog

def get_ministry_dashboard_stats(term=None, days=30):
    """
     aggregations for:
     - Government Spending (Subsidies vs Job Bonuses)
     - Active Players
     - Income (Donations)
     - Ministry Budget Limit
    """
    end_date = timezone.now()
    start_date = end_date - timedelta(days=days)

    # If term is provided, constrain to term dates
    if term:
        if term.start_date > start_date:
            start_date = term.start_date
        if term.end_date and term.end_date < end_date:
            end_date = term.end_date

    stats = {
        'labels': [],
        'spending_subsidies': [],
        'spending_jobs': [],
        'active_players': [],
        'income_donations': [],
        'budget_balance': [],
    }

    # Helper to generate date range
    date_range = []
    current = start_date
    while current <= end_date:
        date_range.append(current.date())
        current += timedelta(days=1)
    
    stats['labels'] = [d.isoformat() for d in date_range] # ISO format for JS

    # 1. Government Spending (All Expenses in GOVERNMENT book)
    expenses_qs = LedgerEntry.objects.filter(
        account__account_type=Account.AccountType.EXPENSE,
        account__book=Account.Book.GOVERNMENT,
        journal_entry__date__range=[start_date.date(), end_date.date()]
    ).annotate(day=TruncDay('journal_entry__date')).values('day', 'journal_entry__description').annotate(
        total=Sum('debit')
    ).order_by('day')

    spending_map = {d: {'subsidies': 0.0, 'jobs': 0.0, 'other': 0.0} for d in date_range}

    for entry in expenses_qs:
        day = entry['day']
        if hasattr(day, 'date'):
            day = day.date()
        desc = entry['journal_entry__description'] or ""
        amount = entry['total']
        
        if day in spending_map:
            if "Subsidy" in desc:
                spending_map[day]['subsidies'] += float(amount)
            elif "Job" in desc or "Escrow" in desc:
                spending_map[day]['jobs'] += float(amount)
            else:
                spending_map[day]['other'] += float(amount)

    stats['spending_subsidies'] = [spending_map[d]['subsidies'] for d in date_range]
    stats['spending_jobs'] = [spending_map[d]['jobs'] for d in date_range]
    stats['spending_other'] = [spending_map[d]['other'] for d in date_range]

    # 2. Active Players
    # DAU (Daily Unique Players) from PlayerStatusLog
    dau_qs = PlayerStatusLog.objects.filter(
        timespan__startswith__range=[start_date, end_date]
    ).annotate(day=TruncDay('timespan__startswith')).values('day').annotate(
        unique_players=Count('character__player', distinct=True)
    ).order_by('day')

    # Peak Concurrent Players from ServerStatus
    from amc.models import ServerStatus
    peak_qs = ServerStatus.objects.filter(
        timestamp__range=[start_date, end_date]
    ).annotate(day=TruncDay('timestamp')).values('day').annotate(
        peak=Max('num_players')
    ).order_by('day')

    dau_map = {d: 0 for d in date_range}
    for entry in dau_qs:
        day = entry['day']
        if hasattr(day, 'date'):
            day = day.date()
        if day in dau_map:
            dau_map[day] = entry['unique_players']

    peak_map = {d: 0 for d in date_range}
    for entry in peak_qs:
        day = entry['day']
        if hasattr(day, 'date'):
            day = day.date()
        if day in peak_map:
            peak_map[day] = entry['peak']
    
    stats['active_players_dau'] = [dau_map[d] for d in date_range]
    stats['active_players_peak'] = [peak_map[d] for d in date_range]

    # Delete old key to avoid confusion
    if 'active_players' in stats:
        del stats['active_players']

    # 3. Income (All Revenue in GOVERNMENT book)
    revenue_qs = LedgerEntry.objects.filter(
        account__account_type=Account.AccountType.REVENUE,
        account__book=Account.Book.GOVERNMENT,
        journal_entry__date__range=[start_date.date(), end_date.date()]
    ).annotate(day=TruncDay('journal_entry__date')).values('day', 'journal_entry__description').annotate(
        total=Sum('credit')
    ).order_by('day')

    income_map = {d: {'donations': 0.0, 'other': 0.0} for d in date_range}
    for entry in revenue_qs:
        day = entry['day']
        if hasattr(day, 'date'):
            day = day.date()
        desc = entry['journal_entry__description'] or ""
        if day in income_map:
            if "Donation" in desc:
                income_map[day]['donations'] += float(entry['total'])
            else:
                income_map[day]['other'] += float(entry['total'])
    
    stats['income_donations'] = [income_map[d]['donations'] for d in date_range]
    stats['income_other'] = [income_map[d]['other'] for d in date_range]

    # 4. Ministry Budget (Running Balance Reconstruction)
    # This is tricky because we only have current balance easily.
    # We need to fetch initial balance or current balance and walk back/forward.
    # Alternatively, fetch all debits/credits for the account and compute running sum.
    # Given we might not have all history efficiently, let's try to reconstruct from start of term if possible,
    # or just show daily net change? The requirement says "ministerial budget (if active)".
    # Let's show the remaining budget at the end of each day.
    
    # Get all budget movements in range
    budget_txs = LedgerEntry.objects.filter(
        account__name='Ministry of Commerce Budget',
        account__book=Account.Book.GOVERNMENT,
        journal_entry__date__lte=end_date.date()
    ).annotate(day=TruncDay('journal_entry__date')).values('day').annotate(
        net_change=Sum(F('debit') - F('credit')) # Asset: Debit +, Credit -
    ).order_by('day')
    
    # Calculate cumulative balance
    # We need the balance BEFORE the start date to initialize the running total
    # But since we are calculating for the *term* mostly, using 0 as start might be wrong if it carried over.
    # However, for a graph, absolute values matter.
    # Optimally: Fetch current balance, and subtract backwards? 
    # Or sum ALL transactions up to start_date.
    
    initial_balance_agg = LedgerEntry.objects.filter(
        account__name='Ministry of Commerce Budget',
        account__book=Account.Book.GOVERNMENT,
        journal_entry__date__lt=start_date.date()
    ).aggregate(balance=Sum(F('debit') - F('credit')))
    
    running_balance = float(initial_balance_agg['balance'] or 0.0)
    
    budget_map = {d: 0.0 for d in date_range}
    
    # Convert txs to map for easy lookup
    daily_changes = {}
    for entry in budget_txs:
        day = entry['day']
        if day:
            if hasattr(day, 'date'):
                day = day.date()
            daily_changes[day] = entry['net_change']
    
    # Iterate through ALL days from start to end to fill holes
    current = start_date.date()
    while current <= end_date.date():
        change = daily_changes.get(current, 0.0)
        running_balance += float(change)
        budget_map[current] = float(running_balance)
        current += timedelta(days=1)
        
    stats['budget_balance'] = [budget_map[d] for d in date_range]

    # 5. Treasury Funds (Running Balance)
    treasury_txs = LedgerEntry.objects.filter(
        account__name='Treasury Fund',
        account__book=Account.Book.GOVERNMENT,
        journal_entry__date__lte=end_date.date()
    ).annotate(day=TruncDay('journal_entry__date')).values('day').annotate(
        net_change=Sum(F('debit') - F('credit'))
    ).order_by('day')

    initial_treasury_agg = LedgerEntry.objects.filter(
        account__name='Treasury Fund',
        account__book=Account.Book.GOVERNMENT,
        journal_entry__date__lt=start_date.date()
    ).aggregate(balance=Sum(F('debit') - F('credit')))

    treasury_running = float(initial_treasury_agg['balance'] or 0.0)
    treasury_map = {d: 0.0 for d in date_range}
    
    daily_treasury_changes = {}
    for entry in treasury_txs:
        day = entry['day']
        if day:
            if hasattr(day, 'date'):
                day = day.date()
            daily_treasury_changes[day] = entry['net_change']

    current = start_date.date()
    while current <= end_date.date():
        change = daily_treasury_changes.get(current, 0.0)
        treasury_running += float(change)
        treasury_map[current] = float(treasury_running)
        current += timedelta(days=1)

    stats['treasury_balance'] = [treasury_map[d] for d in date_range]

    # 6. Job Success vs Failure
    from amc.models import DeliveryJob
    
    success_qs = DeliveryJob.objects.filter(
        fulfilled=True,
        fulfilled_at__range=[start_date, end_date]
    ).annotate(day=TruncDay('fulfilled_at')).values('day').annotate(
        count=Count('id')
    ).order_by('day')

    failed_qs = DeliveryJob.objects.filter(
        fulfilled=False,
        expired_at__range=[start_date, end_date]
    ).filter(expired_at__lt=timezone.now()).annotate(day=TruncDay('expired_at')).values('day').annotate(
        count=Count('id')
    ).order_by('day')

    success_map = {d: 0 for d in date_range}
    for entry in success_qs:
        day = entry['day']
        if hasattr(day, 'date'):
            day = day.date()
        if day in success_map:
            success_map[day] = entry['count']

    failed_map = {d: 0 for d in date_range}
    for entry in failed_qs:
        day = entry['day']
        if hasattr(day, 'date'):
            day = day.date()
        if day in failed_map:
            failed_map[day] = entry['count']

    stats['jobs_success'] = [success_map[d] for d in date_range]
    stats['jobs_failed'] = [failed_map[d] for d in date_range]

    return stats
