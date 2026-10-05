"""Read-only PDF presentation of the existing receivables services."""
from io import BytesIO
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from datetime import datetime
from src.utils import money, brdate, SAO_PAULO


def report_context(db, args, domain):
    from src.services.bar_receivables import receivables as bar
    from src.services.sports_receivables import receivables as sports
    if domain not in {'bar','sports','all'}:
        raise ValueError('Domínio inválido.')
    sections=[]
    if domain in {'bar','all'}: sections.append(('Bar',bar(db,args)))
    if domain in {'sports','all'}: sections.append(('Material Esportivo',sports(db,args)))
    fields=('receivable_cents','overdue_cents','upcoming_cents','received_cents','overdue_count')
    summary={key:sum(context['summary'][key] for _,context in sections) for key in fields}
    summary['players_with_balance']=len({p['player_id'] for _,context in sections for p in context['plans'] if p['player_id'] is not None and p['balance_cents']>0})
    return dict(domain=domain,sections=sections,summary=summary)


def build_receivables_pdf(context):
    output=BytesIO()
    doc=SimpleDocTemplate(output,pagesize=landscape(A4),leftMargin=12*mm,rightMargin=12*mm,
                          topMargin=12*mm,bottomMargin=15*mm,title='Contas a Receber',author='PELADEIROS GPCTA')
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Cell',parent=styles['Normal'],fontSize=8,leading=11))
    styles.add(ParagraphStyle(name='Head',parent=styles['Cell'],fontName='Helvetica-Bold',textColor=colors.white))
    styles.add(ParagraphStyle(name='ReportTitle',parent=styles['Title'],fontSize=18,textColor=colors.HexColor('#073B5C')))
    def cell(value,head=False):return Paragraph(escape(str(value)),styles['Head' if head else 'Cell'])
    def table(rows,widths):
        result=Table(rows,colWidths=[w*mm for w in widths],repeatRows=1,hAlign='LEFT')
        result.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#073B5C')),
                                   ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F4F6F7')]),
                                   ('GRID',(0,0),(-1,-1),.3,colors.HexColor('#CFD8DC')),
                                   ('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
        return result
    subtitle={'bar':'Bar','sports':'Material Esportivo','all':'Consolidado'}[context['domain']]
    story=[Paragraph('PELADEIROS GPCTA',styles['ReportTitle']),Paragraph('CONTAS A RECEBER',styles['Heading1']),
           Paragraph(subtitle,styles['Heading2']),cell('Emissão: '+datetime.now(SAO_PAULO).strftime('%d/%m/%Y %H:%M')),Spacer(1,3*mm)]
    filters=context['sections'][0][1]['filters']
    player=filters['player_id']
    if player:
        player=next((str(p['name']) for _,section in context['sections'] for p in section['players'] if str(p['id'])==player),'#'+player)
    situation={'':'Todos','paid':'Quitado','overdue':'Vencido','open':'Em aberto'}[filters['situation']]
    story.append(cell(f"Filtros: Peladeiro: {player or 'Todos'} | Situação: {situation} | Vencimento: {brdate(filters['due_from']) if filters['due_from'] else 'Sem início'} a {brdate(filters['due_to']) if filters['due_to'] else 'Sem fim'}"))
    story.append(Spacer(1,4*mm))
    labels=[('A receber','receivable_cents'),('Vencido','overdue_cents'),('A vencer','upcoming_cents'),('Recebido','received_cents'),('Peladeiros com saldo','players_with_balance'),('Parcelas vencidas','overdue_count')]
    story.append(table([[cell(label,True) for label,_ in labels],
                        [cell(money(context['summary'][key]) if key.endswith('_cents') else context['summary'][key]) for _,key in labels]], [45.5]*6))
    for name,section in context['sections']:
        story.extend([Spacer(1,5*mm),Paragraph(name.upper(),styles['Heading2'])])
        headers=('Peladeiro','Venda/Compra','Total','Recebido','Saldo','Próxima parcela','Vencimento','Situação')
        rows=[[cell(h,True) for h in headers]]
        for plan in section['plans']:
            pending=sorted((i for i in plan['installments'] if i['status']=='pending'),key=lambda i:(str(i['due_date']),i['installment_number']))
            next_number=str(pending[0]['installment_number']) if pending else '-'
            rows.append([cell(v) for v in (plan['player_name'],'#'+str(plan['sale_id']),money(plan['total_cents']),money(plan['received_cents']),money(plan['balance_cents']),next_number,brdate(plan['next_due_date']) if plan['next_due_date'] else '-',plan['situation_label'])])
        if section['plans']:story.append(table(rows,[57,25,30,30,30,28,34,39]))
        else:story.append(cell('Nenhum parcelamento corresponde aos filtros.'))
    def footer(canvas,document):
        canvas.saveState();canvas.setFont('Helvetica',8)
        canvas.drawString(12*mm,8*mm,'PELADEIROS GPCTA - Consulta somente leitura')
        canvas.drawRightString(285*mm,8*mm,f'Página {document.page}');canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    output.seek(0)
    return output
