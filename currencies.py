"""Supported two-decimal currencies; stored amounts remain integer hundredths."""
SUPPORTED = {'CAD':'Canadian dollar','USD':'US dollar','EUR':'Euro','GBP':'British pound','AUD':'Australian dollar','NZD':'New Zealand dollar','CHF':'Swiss franc','CNY':'Chinese yuan','HKD':'Hong Kong dollar','SGD':'Singapore dollar','INR':'Indian rupee','MXN':'Mexican peso','BRL':'Brazilian real','ZAR':'South African rand','SEK':'Swedish krona','NOK':'Norwegian krone','DKK':'Danish krone','PLN':'Polish zloty','CZK':'Czech koruna','AED':'UAE dirham','SAR':'Saudi riyal','TRY':'Turkish lira','PHP':'Philippine peso','THB':'Thai baht','MYR':'Malaysian ringgit'}
def get(c,default):
    row=c.execute("SELECT value FROM preferences WHERE key='currency'").fetchone()
    return row[0] if row else default

def set_currency(c,code):
    if code not in SUPPORTED: raise ValueError('Choose a supported currency.')
    c.execute("INSERT INTO preferences VALUES ('currency',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(code,))
    c.execute('DELETE FROM restore_previews')
