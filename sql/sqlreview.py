# -*- coding: UTF-8 -*-
import json

import time
from django.db import connection
from django.utils import timezone
from django.conf import settings

from .dao import Dao
from .const import Const, WorkflowDict
from .sendmail import MailSender
from .inception import InceptionDao
from .aes_decryptor import Prpcrypt
from .models import users, workflow, master_config
from .workflow import Workflow
from .permission import role_required, superuser_required
import logging

logger = logging.getLogger('default')

dao = Dao()
inceptionDao = InceptionDao()
mailSender = MailSender()
prpCryptor = Prpcrypt()
workflowOb = Workflow()


# 获取当前请求url
def _getDetailUrl(request):
    scheme = request.scheme
    host = request.META['HTTP_HOST']
    return "%s://%s/detail/" % (scheme, host)


# 根据实例名获取主库连接字符串，并封装成一个dict
def getMasterConnStr(clusterName):
    listMasters = master_config.objects.filter(cluster_name=clusterName)

    masterHost = listMasters[0].master_host
    masterPort = listMasters[0].master_port
    masterUser = listMasters[0].master_user
    masterPassword = prpCryptor.decrypt(listMasters[0].master_password)
    dictConn = {'masterHost': masterHost, 'masterPort': masterPort, 'masterUser': masterUser,
                'masterPassword': masterPassword}
    return dictConn


# SQL工单跳过inception执行回调
def _execute_skipinc_call_back(workflowId, clusterName, sql_content, url):
    workflowDetail = workflow.objects.get(id=workflowId)
    # 获取审核人
    reviewMan = workflowDetail.review_man

    # 获取实例连接信息
    masterInfo = getMasterConnStr(clusterName)
    try:
        # 执行sql
        t_start = time.time()
        execute_result = dao.mysql_execute(masterInfo['masterHost'], masterInfo['masterPort'], masterInfo['masterUser'],
                                           masterInfo['masterPassword'], sql_content)
        t_end = time.time()
        execute_time = "%5s" % "{:.4f}".format(t_end - t_start)
        execute_result['execute_time'] = execute_time + 'sec'

        workflowDetail = workflow.objects.get(id=workflowId)
        if execute_result.get('Warning'):
            workflowDetail.status = Const.workflowStatus['exception']
        elif execute_result.get('Error'):
            workflowDetail.status = Const.workflowStatus['exception']
        else:
            workflowDetail.status = Const.workflowStatus['finish']
        workflowDetail.finish_time = timezone.now()
        workflowDetail.execute_result = json.dumps(execute_result)
        workflowDetail.is_manual = 1
        workflowDetail.audit_remark = ''
        workflowDetail.is_backup = '否'
        # 重新获取连接，防止超时
        connection.close()
        workflowDetail.save()
    except Exception as e:
        logger.error(e)

    # 如果执行完毕了，则根据settings.py里的配置决定是否给提交者和DBA一封邮件提醒，DBA需要知晓审核并执行过的单子
    if getattr(settings, 'MAIL_ON_OFF'):
        engineer = workflowDetail.engineer
        workflowStatus = workflowDetail.status
        workflowName = workflowDetail.workflow_name
        strTitle = "SQL上线工单执行完毕 # " + str(workflowId)
        strContent = "发起人：" + engineer + "\n审核人：" + reviewMan + "\n工单地址：" + url \
                     + "\n工单名称： " + workflowName + "\n执行结果：" + workflowStatus
        # 邮件通知申请人，审核人，抄送DBA
        notify_users = reviewMan.split(',')
        notify_users.append(engineer)
        listToAddr= [email['email'] for email in users.objects.filter(username__in=notify_users).values('email')]
        listCcAddr = [email['email'] for email in users.objects.filter(role='DBA').values('email')]
        mailSender.sendEmail(strTitle, strContent, listToAddr, listCcAddr=listCcAddr)


# SQL工单执行回调
def _execute_call_back(workflowId, clusterName, url):
    workflowDetail = workflow.objects.get(id=workflowId)
    # 获取审核人
    reviewMan = workflowDetail.review_man

    dictConn = getMasterConnStr(clusterName)
    try:
        # 交给inception先split，再执行
        (finalStatus, finalList) = inceptionDao.executeFinal(workflowDetail, dictConn)

        # 封装成JSON格式存进数据库字段里
        strJsonResult = json.dumps(finalList)
        workflowDetail = workflow.objects.get(id=workflowId)
        workflowDetail.execute_result = strJsonResult
        workflowDetail.finish_time = timezone.now()
        workflowDetail.status = finalStatus
        workflowDetail.is_manual = 0
        workflowDetail.audit_remark = ''
        # 重新获取连接，防止超时
        connection.close()
        workflowDetail.save()
    except Exception as e:
        logger.error(e)

    # 如果执行完毕了，则根据settings.py里的配置决定是否给提交者和DBA一封邮件提醒，DBA需要知晓审核并执行过的单子
    if getattr(settings, 'MAIL_ON_OFF'):
        # 给申请人，DBA各发一封邮件
        engineer = workflowDetail.engineer
        workflowStatus = workflowDetail.status
        workflowName = workflowDetail.workflow_name
        strTitle = "SQL上线工单执行完毕 # " + str(workflowId)
        strContent = "发起人：" + engineer + "\n审核人：" + reviewMan + "\n工单地址：" + url \
                     + "\n工单名称： " + workflowName + "\n执行结果：" + workflowStatus
        # 邮件通知申请人，审核人，抄送DBA
        notify_users = reviewMan.split(',')
        notify_users.append(engineer)
        listToAddr= [email['email'] for email in users.objects.filter(username__in=notify_users).values('email')]
        listCcAddr = [email['email'] for email in users.objects.filter(role='DBA').values('email')]
        mailSender.sendEmail(strTitle, strContent, listToAddr, listCcAddr=listCcAddr)
