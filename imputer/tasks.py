"""
Asynchronous tasks that update data in Open Humans.
These tasks:
  1. delete any current files in OH if they match the planned upload filename
  2. adds a data file
"""
import os
import logging
import requests
from celery import chain, group
from subprocess import run, PIPE
from ohapi import api
from os import environ
import pandas as pd
from django.conf import settings
from open_humans.models import OpenHumansMember
from datauploader.tasks import process_source
from openhumansimputer.settings import CHROMOSOMES
from imputer.models import ImputerMember
import bz2
import gzip
from shutil import copyfileobj
from itertools import takewhile
import time
import datetime
from openhumansimputer.celery import app


HOME = environ.get('HOME')
IMP_BIN = environ.get('IMP_BIN')
REF_PANEL = environ.get('REF_PANEL')
REF_PANEL_X = environ.get('REF_PANEL_X')
DATA_DIR = environ.get('DATA_DIR')
REF_FA = environ.get('REF_FA')
OUT_DIR = environ.get('OUT_DIR')

# Set up logging.
logger = logging.getLogger('oh')


@app.task(ignore_result=False)
def submit_chrom(chrom, oh_id, num_submit=0, **kwargs):
    """
    Build and run the genipe-launcher command in subprocess run.
    rate_limit='1/m' sets the number of tasks per minute.
    This is important because impute2 writes a file to a shared directory that
    genipe-launcher tries to delete. If multiple tasks launch at the same time,
    celery task silently fails.
    """
    # this silly block of code runs impute2 because genipe-launcher deletes
    # two unneccesary files before they are available.
    imputer_record = ImputerMember.objects.get(oh_id=oh_id, active=True)
    imputer_record.step = 'submit_chrom'
    imputer_record.save()

    os.makedirs('{}/{}/chr{}'.format(OUT_DIR,
                                     oh_id, chrom), exist_ok=True)
    os.chdir('{}/{}/chr{}'.format(OUT_DIR, oh_id, chrom))
    if chrom == '23':
        command = [
            'genipe-launcher',
            '--chrom', '{}'.format(chrom),
            '--bfile', '{}/{}/member.{}.plink.gt'.format(
                DATA_DIR, oh_id, oh_id),
            '--shapeit-bin', '{}/shapeit'.format(IMP_BIN),
            '--impute2-bin', '{}/impute2'.format(IMP_BIN),
            '--plink-bin', '{}/plink'.format(IMP_BIN),
            '--reference', '{}/hg19.fasta'.format(REF_FA),
            '--hap-nonPAR', '{}/1000GP_Phase3_chrX_NONPAR.hap.gz'.format(REF_PANEL_X),
            '--hap-PAR1', '{}/1000GP_Phase3_chrX_PAR1.hap.gz'.format(REF_PANEL_X),
            '--hap-PAR2', '{}/1000GP_Phase3_chrX_PAR2.hap.gz'.format(REF_PANEL_X),
            '--legend-nonPAR', '{}/1000GP_Phase3_chrX_NONPAR.legend.gz'.format(REF_PANEL_X),
            '--legend-PAR1', '{}/1000GP_Phase3_chrX_PAR1.legend.gz'.format(REF_PANEL_X),
            '--legend-PAR2', '{}/1000GP_Phase3_chrX_PAR2.legend.gz'.format(REF_PANEL_X),
            '--map-nonPAR', '{}/genetic_map_chrX_nonPAR_combined_b37.txt'.format(REF_PANEL_X),
            '--map-PAR1', '{}/genetic_map_chrX_PAR1_combined_b37.txt'.format(REF_PANEL_X),
            '--map-PAR2', '{}/genetic_map_chrX_PAR2_combined_b37.txt'.format(REF_PANEL_X),
            '--sample-file', '{}/1000GP_Phase3.sample'.format(REF_PANEL),
            '--map-template', '{}/genetic_map_chrX_nonPAR_combined_b37.txt'.format(REF_PANEL_X),
            '--legend-template', '{}/1000GP_Phase3_chrX_NONPAR.legend.gz'.format(REF_PANEL_X),
            '--hap-template', '{}/1000GP_Phase3_chrX_NONPAR.hap.gz'.format(REF_PANEL_X),
            '--filtering-rules', 'ALL<0.01', 'ALL>0.99',
            '--segment-length', '5e+06',
            '--impute2-extra', '-nind 1',
            '--report-title', '"Test"',
            '--report-number', '"Test Report"',
            '--output-dir', '{}/{}/chr{}'.format(OUT_DIR,
                                                 oh_id, chrom),
            '--shapeit-extra', '-R {}/1000GP_Phase3_chrX_NONPAR.hap.gz {}/1000GP_Phase3_chrX_NONPAR.legend.gz {}/1000GP_Phase3.sample --exclude-snp {}/{}/chr{}/chr{}/chr{}.alignments.snp.strand.exclude'.format(
                REF_PANEL_X, REF_PANEL_X, REF_PANEL, OUT_DIR, oh_id, chrom, chrom, chrom)
        ]
    else:
        command = [
            'genipe-launcher',
            '--chrom', '{}'.format(chrom),
            '--bfile', '{}/{}/member.{}.plink.gt'.format(
                DATA_DIR, oh_id, oh_id),
            '--shapeit-bin', '{}/shapeit'.format(IMP_BIN),
            '--impute2-bin', '{}/impute2'.format(IMP_BIN),
            '--plink-bin', '{}/plink'.format(IMP_BIN),
            '--reference', '{}/hg19.fasta'.format(REF_FA),
            '--hap-template', '{}/1000GP_Phase3_chr{}.hap.gz'.format(
                REF_PANEL, chrom),
            '--legend-template', '{}/1000GP_Phase3_chr{}.legend.gz'.format(
                REF_PANEL, chrom),
            '--map-template', '{}/genetic_map_chr{}_combined_b37.txt'.format(
                REF_PANEL, chrom),
            '--sample-file', '{}/1000GP_Phase3.sample'.format(REF_PANEL),
            '--filtering-rules', 'ALL<0.01', 'ALL>0.99',
            '--segment-length', '5e+06',
            '--impute2-extra', '-nind 1',
            '--report-title', '"Test"',
            '--report-number', '"Test Report"',
            '--output-dir', '{}/{}/chr{}'.format(OUT_DIR,
                                                 oh_id, chrom),
            '--shapeit-extra', '-R {}/1000GP_Phase3_chr{}.hap.gz {}/1000GP_Phase3_chr{}.legend.gz {}/1000GP_Phase3.sample --exclude-snp {}/{}/chr{}/chr{}/chr{}.alignments.snp.strand.exclude'.format(
                REF_PANEL, chrom, REF_PANEL, chrom, REF_PANEL, OUT_DIR, oh_id, chrom, chrom, chrom)
        ]
    run(command, stdout=PIPE, stderr=PIPE)


@app.task()
def get_vcf(data_source_id, oh_id):
    """Download member .vcf."""
    oh_member = OpenHumansMember.objects.get(oh_id=oh_id)
    imputer_record = ImputerMember.objects.get(oh_id=oh_id, active=True)
    imputer_record.step = 'get_vcf'
    imputer_record.save()
    logger.info('Downloading vcf for member {}'.format(oh_id))
    user_details = api.exchange_oauth2_member(oh_member.get_access_token())
    for data_source in user_details['data']:
        if str(data_source['id']) == str(data_source_id):
            data_file_url = data_source['download_url']
    datafile = requests.get(data_file_url)
    os.makedirs('{}/{}'.format(DATA_DIR, oh_id), exist_ok=True)
    with open('{}/{}/member.{}.vcf'.format(DATA_DIR, oh_id, oh_id), 'wb') as handle:
        try:
            textobj = bz2.decompress(datafile.content)
            handle.write(textobj)
        except OSError:
            textobj = gzip.decompress(datafile.content)
            handle.write(textobj)
        except OSError:
            for block in datafile.iter_content(1024):
                handle.write(block)
        except:
            logger.critical('your data source file is malformated')
    time.sleep(5)  # download takes a few seconds


@app.task
def prepare_data(oh_id):
    """Process the member's .vcf."""
    imputer_record = ImputerMember.objects.get(oh_id=oh_id, active=True)
    imputer_record.step = 'prepare_data'
    imputer_record.save()
    os.chdir(settings.BASE_DIR)

    command = [
        'imputer/prepare_genotypes.sh', '{}'.format(oh_id)
    ]
    process = run(command, stdout=PIPE, stderr=PIPE)
    if process.stderr:
        logger.debug(process.stderr)
    logger.info('finished preparing {} plink data'.format(oh_id))


def _rreplace(s, old, new, occurrence):
    li = s.rsplit(old, occurrence)
    return new.join(li)


@app.task(ignore_result=False)
def process_chrom(chrom, oh_id, num_submit=0, **kwargs):
    """
    1. read .impute2 files (w/ genotype probabilities)
    2. read .impute2_info files (with "info" field for filtering)
    3. filter the genotypes in .impute2_info
    4. merge on right (.impute2_info), acts like a filter for the left.
    """
    print('{} Imputation has completed, now processing results.'.format(oh_id))
    impute_cols = ['chr', 'name', 'position',
                   'a0', 'a1', 'a0a0_p', 'a0a1_p', 'a1a1_p']

    df = pd.DataFrame()
    df_gp = pd.DataFrame()  # hold genotype probabilities
    df_impute = pd.read_csv('{}/{}/chr{}/chr{}/final_impute2/'
                            'chr{}.imputed.impute2'
                            .format(OUT_DIR, oh_id, chrom, chrom, chrom),
                            sep=' ',
                            header=None,
                            names=impute_cols)

    df_info = pd.read_csv(
        '{}/{}/chr{}/chr{}/final_impute2/chr{}.imputed.impute2_info'.format(
            OUT_DIR, oh_id, chrom, chrom, chrom),
        sep='\t')

    # combine impute2 and impute2_info to induce filter
    df = df_impute.merge(
        df_info, on=['chr', 'position', 'a0', 'a1'], how='right')
    df.rename(columns={'name_x': 'name'}, inplace=True)
    df_gp = pd.concat([df_gp, df])
    df = df[impute_cols]

    df_gp['name'] = df_gp['name'].apply(_rreplace, args=(':', '_', 2))
    df['name'] = df['name'].apply(_rreplace, args=(':', '_', 2))

    df_gp.to_csv('{}/{}/chr{}/chr{}/final_impute2/chr{}.imputed.impute2.GP'.format(OUT_DIR, oh_id, chrom, chrom, chrom),
                 header=True,
                 index=False,
                 sep=' ')

    # dump all chromosomes as an .impute2
    df.to_csv('{}/{}/chr{}/chr{}/final_impute2/chr{}.imputed.impute2'.format(OUT_DIR, oh_id, chrom, chrom, chrom),
              header=False,
              index=False,
              sep=' ')
    # don't need this huge dataframe anymore
    del df

    # convert to vcf
    os.chdir(settings.BASE_DIR)
    output_vcf_cmd = [
        'imputer/output_vcf.sh', '{}'.format(oh_id), '{}'.format(chrom)
    ]
    process = run(output_vcf_cmd, stdout=PIPE, stderr=PIPE)
    if process.stderr:
        logger.debug(process.stderr)

    # annotate genotype probabilities and info metric
    vcf_file = '{}/{}/chr{}/chr{}/final_impute2/chr{}.member.imputed.vcf'.format(
        OUT_DIR, oh_id, chrom, chrom, chrom)
    cols = ['CHROM', 'POS', 'ID', 'REF', 'ALT',
            'QUAL', 'FILTER', 'INFO', 'FORMAT', 'MEMBER']

    # capture header
    if chrom == '5':
        with open(vcf_file, 'r') as vcf:
            headiter = takewhile(lambda s: s.startswith('#'), vcf)
            header = list(headiter)
            with open('{}/{}/header.txt'.format(OUT_DIR, oh_id), 'w') as headerobj:
                headerobj.write(''.join(header))

    dfvcf = pd.read_csv(vcf_file, sep='\t', header=None,
                        comment='#', names=cols)

    df_gp.rename(columns={'name': 'ID'}, inplace=True)
    df_gp.set_index(['ID'], inplace=True)
    dfvcf.set_index(['ID'], inplace=True)
    dfvcf = dfvcf.merge(
        df_gp[['a0a0_p', 'a0a1_p', 'a1a1_p', 'info']], left_index=True, right_index=True)
    del df_gp
    # currently using custom annotation for "dosage"
    dfvcf['MEMBER'] = dfvcf['MEMBER'] + ':' + dfvcf['a0a0_p'].round(3).astype(
        str) + ',' + dfvcf['a0a1_p'].round(3).astype(str) + ',' + dfvcf['a1a1_p'].round(3).astype(str)
    dfvcf['FORMAT'] = dfvcf['FORMAT'].astype(str) + ':GP'
    dfvcf['INFO'] = dfvcf['INFO'].astype(
        str) + ';INFO=' + dfvcf['info'].round(3).astype(str)
    dfvcf.reset_index(inplace=True)

    dfvcf[cols].to_csv(vcf_file, sep='\t',
                       header=None, index=False)


@app.task(ignore_result=False)
def upload_to_oh(oh_id):
    logger.info('{}: now uploading to OpenHumans'.format(oh_id))

    with open('{}/{}/header.txt'.format(OUT_DIR, oh_id), 'r') as headerobj:
        headiter = takewhile(lambda s: s.startswith('#'), headerobj)
        header = list(headiter)

    # construct the final header
    new_header = ['##FORMAT=<ID=GP,Number=3,Type=Float,Description="Estimated Posterior Probabilities (rounded to 3 digits) for Genotypes 0/0, 0/1 and 1/1">\n',
                  '##INFO=<ID=INFO,Number=1,Type=Float,Description="Impute2 info metric">\n',
                  '##imputerdate={}\n'.format(
                      datetime.date.today().strftime("%m-%d-%y"))
                  ]
    header.insert(-2, new_header[0])
    header.insert(-4, new_header[1])
    header.insert(1, new_header[2])
    header = ''.join(header)

    # combine all vcfs
    os.chdir(settings.BASE_DIR)
    combine_command = [
        'imputer/combine_chrom.sh', '{}'.format(oh_id)
    ]
    process = run(combine_command, stdout=PIPE, stderr=PIPE)
    logger.debug(process.stderr)

    member_vcf_fp = '{}/{}/member.imputed.vcf'.format(OUT_DIR, oh_id)
    # add the header to the combined vcf file.
    with open(member_vcf_fp, 'r') as original:
        data = original.read()
    with open(member_vcf_fp, 'w') as modified:
        modified.write(header + data)

    # bzip the file
    with open(member_vcf_fp, 'rb') as input_:
        with bz2.BZ2File(member_vcf_fp + '.bz2', 'wb', compresslevel=9) as output:
            copyfileobj(input_, output)

    # upload file to OpenHumans
    process_source(oh_id)

    # Message Member
    oh_member = OpenHumansMember.objects.get(oh_id=oh_id)
    project_page = environ.get('OH_ACTIVITY_PAGE')
    api.message('Open Humans Imputation Complete',
                'Check {} to see your imputed genotype results from Open Humans.'.format(
                    project_page),
                oh_member.access_token,
                project_member_ids=[oh_id])
    logger.info('{} emailed member'.format(oh_id))

    # clean users files
    if not settings.DEBUG:
        os.chdir(settings.BASE_DIR)
        clean_command = [
            'imputer/clean_files.sh', '{}'.format(oh_id)
        ]
        process = run(clean_command, stdout=PIPE, stderr=PIPE)
        logger.debug(process.stderr)
        logger.info('{} finished removing files'.format(oh_id))

    imputer_record = ImputerMember.objects.get(oh_id=oh_id, active=True)
    imputer_record.step = 'complete'
    imputer_record.active = False
    imputer_record.save()


def pipeline(vcf_id, oh_id):
    task1 = get_vcf.si(vcf_id, oh_id)
    task2 = prepare_data.si(oh_id)
    async_chroms = group(submit_chrom.si(chrom, oh_id)
                         for chrom in CHROMOSOMES)
    async_process = group(process_chrom.si(chrom, oh_id)
                          for chrom in CHROMOSOMES)
    task3 = chain(async_chroms, async_process, upload_to_oh.si(oh_id))

    pipeline = chain(task1, task2, task3)
    pipeline.apply_async()
