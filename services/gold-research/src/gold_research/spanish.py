"""Conservative, explicit Spanish price layout. Never infer a missing level."""
import re
import unicodedata

NUMBER = r'\d+(?:[.,]\d+)?'


def parse_spanish(text):
    plain = ''.join(c for c in unicodedata.normalize('NFKD', text.upper()) if not unicodedata.combining(c))
    active = bool(re.search(r'\b(COMPRA|COMPRAR|VENTA|VENDER|ENTRADA|OBJETIVO|ORO)\b', plain))
    if not active:
        return False, None, []
    errors = []
    directions = set(re.findall(r'\b(COMPRA|COMPRAR|VENTA|VENDER|BUY|SELL)\b', plain))
    directions = {'BUY' if value in ('COMPRA','COMPRAR','BUY') else 'SELL' for value in directions}
    if len(directions) != 1 or re.search(r'\b(NO|SI|ESPERAR|ESPERA|POSIBLE|CANCELAR|CANCELADA)\b', plain):
        return True, None, ['Ambiguous, conditional or cancelled direction; manual review required']
    # Thousands separators and decimal commas with three trailing digits are ambiguous.
    if re.search(r'\d+[.,]\d+[.,]\d+|\d+,\d{3}\b', plain):
        return True, None, ['Ambiguous numeric separators; manual review required']
    def number(value):
        return float(value.replace(',', '.'))
    entries = re.findall(r'\b(?:ENTRADA|ZONA(?: DE ENTRADA)?)\s*[:=@]?\s*('+NUMBER+r')(?:\s*(?:[-–—/]|A)\s*('+NUMBER+r'))?', plain)
    if not entries:
        entries = re.findall(r'\b(?:COMPRA|COMPRAR|VENTA|VENDER|BUY|SELL)\s+(?:(?:ORO|GOLD|XAU\s*/?\s*USDM?)\s*)?(?:AHORA\s*)?(?:EN|@)\s*('+NUMBER+r')(?:\s*[-–—/]\s*('+NUMBER+r'))?', plain)
    stops = re.findall(r'\b(?:SL|STOP\s*LOSS|STOP|PERDIDA MAXIMA)\s*[:=]?\s*('+NUMBER+r')', plain)
    targets = re.findall(r'\b(?:TP|OBJETIVO|TAKE\s*PROFIT)\s*([1-8])?(?:\s*[:=]\s*|\s+)('+NUMBER+r')', plain)
    if len(entries) != 1 or len(stops) != 1 or not targets:
        return True, None, ['Exactly one explicit entry, stop loss and at least one labelled target are required']
    low, high = entries[0]
    signal = {'direction': directions.pop(), 'entry_low': min(number(low), number(high or low)),
              'entry_high': max(number(low), number(high or low)), 'stop_loss': number(stops[0])}
    for index, (label, value) in enumerate(targets, 1):
        key = f'tp{label or index}'
        if key in signal or int(label or index) > 8:
            errors.append('Duplicate or excessive target labels')
        signal[key] = number(value)
    for n in range(1, 9):
        signal.setdefault(f'tp{n}', None)
    return True, signal, errors
